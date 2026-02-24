import configparser
import random
import sys
import threading
import time
from concurrent import futures

import grpc

import raft_pb2
import raft_pb2_grpc


class LogEntry:
    def __init__(self, term, key, value):
        self.term = term
        self.key = key
        self.value = value


class KeyValueStoreServicer(raft_pb2_grpc.KeyValueStoreServicer):
    def __init__(self, server_id, peer_ids, base_port=9001):
        self.server_id = server_id
        self.peer_ids = peer_ids
        self.base_port = base_port

        # Assignment 2 state
        self.storage = {}
        self.store_lock = threading.RLock()

        # Assignment 3 Raft state
        self.state_lock = threading.RLock()
        self.currentTerm = 0
        self.votedFor = None
        self.role = "follower"
        self.leaderId = None

        self.stop_event = threading.Event()
        self.election_deadline = 0.0
        self.rng = random.Random(time.time_ns() ^ (server_id << 16))
        self._reset_election_deadline()

        self.election_thread = threading.Thread(target=self._run_election_loop, daemon=True)
        self.heartbeat_thread = threading.Thread(target=self._run_heartbeat_loop, daemon=True)
        self.election_thread.start()
        self.heartbeat_thread.start()

        # Assignment 4 states
        self.log = [None]
        self.commit_index = 0
        self.last_applied = 0
        self.match_index = {peer_id: 0 for peer_id in peer_ids}
        self.next_index = {peer_id: 1 for peer_id in peer_ids}
        self.apply_cv = threading.Condition(self.state_lock)

    def _reset_election_deadline(self):
        self.election_deadline = time.time() + self.rng.uniform(0.150, 0.300)

    def _become_follower(self, new_term, leader_id=None):
        self.currentTerm = new_term
        self.role = "follower"
        self.leaderId = leader_id
        self.votedFor = None
        self._reset_election_deadline()
        self.apply_cv.notify_all()

    def _become_leader(self):
        self.role = "leader"
        self.leaderId = self.server_id
        # Initialize replication state when becoming leader.
        next_idx = len(self.log)
        for peer in self.peer_ids:
            self.next_index[peer] = next_idx
            self.match_index[peer] = 0

    def _apply_committed_entries(self):
        with self.store_lock:
            while self.last_applied < self.commit_index:
                self.last_applied += 1
                entry = self.log[self.last_applied]
                self.storage[entry.key] = entry.value

    def _advance_commit_index(self):
        for i in range(len(self.log) - 1, self.commit_index, -1):
            if self.log[i].term != self.currentTerm:
                continue
            # Leader itself counts as one replica.
            count = 1
            for peer in self.match_index:
                if self.match_index[peer] >= i:
                    count += 1
            if count >= self._cluster_majority():
                self.commit_index = i
                self._apply_committed_entries()
                self.apply_cv.notify_all()
                break

    def _cluster_majority(self):
        return (len(self.peer_ids) + 1) // 2 + 1

    def _request_vote(self, peer, election_term):
        try:
            channel = grpc.insecure_channel(f"localhost:{self.base_port + peer}")
            stub = raft_pb2_grpc.KeyValueStoreStub(channel)
            return stub.RequestVote(
                raft_pb2.RequestVoteArgs(
                    term=election_term,
                    candidateId=self.server_id,
                    lastLogIndex=0,
                    lastLogTerm=0,
                ),
                timeout=0.3,
            )
        except Exception:
            return None

    def _run_election_loop(self):
        while not self.stop_event.is_set():
            time.sleep(0.02)

            with self.state_lock:
                if self.role == "leader" or time.time() < self.election_deadline:
                    continue

                self.role = "candidate"
                self.currentTerm += 1
                election_term = self.currentTerm
                self.votedFor = self.server_id
                self.leaderId = None
                votes = 1
                self._reset_election_deadline()

            vote_reqs = []
            with futures.ThreadPoolExecutor(max_workers=max(1, len(self.peer_ids))) as pool:
                for peer in self.peer_ids:
                    vote_reqs.append(pool.submit(self._request_vote, peer, election_term))

                for req in futures.as_completed(vote_reqs):
                    try:
                        reply = req.result()
                    except Exception:
                        continue

                    if reply is None:
                        continue

                    with self.state_lock:
                        if self.role != "candidate" or self.currentTerm != election_term:
                            break
                        if reply.term > self.currentTerm:
                            self._become_follower(reply.term)
                            break
                        if reply.voteGranted:
                            votes += 1
                            if votes >= self._cluster_majority():
                                self._become_leader()
                                break

    def _run_heartbeat_loop(self):
        while not self.stop_event.is_set():
            time.sleep(0.05)

            with self.state_lock:
                if self.role != "leader":
                    continue
                heartbeat_term = self.currentTerm

            for peer in self.peer_ids:
                try:
                    with self.state_lock:
                        if self.role != "leader" or self.currentTerm != heartbeat_term:
                            break
                        prev_idx = self.next_index[peer] - 1
                        prev_term = self.log[prev_idx].term if prev_idx > 0 else 0
                        entries = self.log[self.next_index[peer] :]
                        leader_commit = self.commit_index

                    channel = grpc.insecure_channel(f"localhost:{self.base_port + peer}")
                    stub = raft_pb2_grpc.KeyValueStoreStub(channel)
                    reply = stub.AppendEntries(
                        raft_pb2.AppendEntriesArgs(
                            term=heartbeat_term,
                            leaderId=self.server_id,
                            prevLogIndex=prev_idx,
                            prevLogTerm=prev_term,
                            entries=entries,
                            leaderCommit=leader_commit,
                        ),
                        timeout=0.3,
                    )

                    with self.state_lock:
                        if self.role != "leader" or self.currentTerm != heartbeat_term:
                            break
                        if reply.term > self.currentTerm:
                            self._become_follower(reply.term)
                            break
                        if reply.success:
                            sent = len(entries)
                            self.match_index[peer] = prev_idx + sent
                            self.next_index[peer] = self.match_index[peer] + 1
                            self._advance_commit_index()
                        else:
                            self.next_index[peer] = max(1, self.next_index[peer] - 1)
                except Exception:
                    continue

    def ping(self, request, context):
        return raft_pb2.GenericResponse(success=True)

    def GetState(self, request, context):
        with self.state_lock:
            return raft_pb2.State(
                term=self.currentTerm,
                isLeader=(self.role == "leader"),
                commitIndex=self.commit_index,
                lastApplied=self.last_applied,
            )

    def RequestVote(self, request, context):
        with self.state_lock:
            if request.term < self.currentTerm:
                return raft_pb2.RequestVoteReply(term=self.currentTerm, voteGranted=False)

            if request.term > self.currentTerm:
                self._become_follower(request.term)

            can_vote = self.votedFor is None or self.votedFor == request.candidateId
            if can_vote:
                self.votedFor = request.candidateId
                self._reset_election_deadline()
                return raft_pb2.RequestVoteReply(term=self.currentTerm, voteGranted=True)

            return raft_pb2.RequestVoteReply(term=self.currentTerm, voteGranted=False)

    def AppendEntries(self, request, context):
        with self.state_lock:
            if request.term < self.currentTerm:
                return raft_pb2.AppendEntriesReply(term=self.currentTerm, success=False)

            # Valid leader heartbeat/replication.
            if request.term > self.currentTerm or self.role != "follower":
                self._become_follower(request.term, leader_id=request.leaderId)
            else:
                self.leaderId = request.leaderId
                self._reset_election_deadline()

            # Consistency check.
            if request.prevLogIndex >= len(self.log):
                return raft_pb2.AppendEntriesReply(term=self.currentTerm, success=False)
            if request.prevLogIndex > 0 and self.log[request.prevLogIndex].term != request.prevLogTerm:
                return raft_pb2.AppendEntriesReply(term=self.currentTerm, success=False)

            # Conflict handling + append.
            idx = request.prevLogIndex + 1
            for entry in request.entries:
                if idx < len(self.log):
                    if self.log[idx].term != entry.term:
                        self.log = self.log[:idx]
                        self.log.append(entry)
                else:
                    self.log.append(entry)
                idx += 1

            # Follow leader commit index and apply.
            if request.leaderCommit > self.commit_index:
                self.commit_index = min(request.leaderCommit, len(self.log) - 1)
                self._apply_committed_entries()
                self.apply_cv.notify_all()

            return raft_pb2.AppendEntriesReply(term=self.currentTerm, success=True)

    def Get(self, request, context):
        key = request.arg
        with self.store_lock:
            value = self.storage.get(key, "")
        return raft_pb2.KeyValue(key=key, value=value)

    def Put(self, request, context):
        with self.state_lock:
            if self.role != "leader":
                return raft_pb2.GenericResponse(success=False, error="Not leader")

            log_entry = raft_pb2.LogEntry(
                term=self.currentTerm,
                key=request.key,
                value=request.value,
                clientId=request.clientId,
                requestId=request.requestId,
            )
            self.log.append(log_entry)
            my_index = len(self.log) - 1

        # Wait until committed, but fail fast if leadership is lost.
        while True:
            with self.state_lock:
                if self.role != "leader":
                    return raft_pb2.GenericResponse(success=False, error="Not leader")
                if self.commit_index >= my_index:
                    return raft_pb2.GenericResponse(success=True)
                self.apply_cv.wait(timeout=0.05)


def _read_active_server_ids(config_path="config.ini"):
    parser = configparser.ConfigParser()
    if not parser.read(config_path):
        return [0, 1, 2, 3, 4]

    try:
        active = parser.get("Servers", "active")
        ids = [int(x.strip()) for x in active.split(",") if x.strip()]
        return ids if ids else [0, 1, 2, 3, 4]
    except Exception:
        return [0, 1, 2, 3, 4]


if __name__ == "__main__":
    server_id = int(sys.argv[1])

    # Optional second argument lets frontend control cluster size for tests.
    if len(sys.argv) >= 3:
        cluster_size = int(sys.argv[2])
        active_ids = list(range(cluster_size))
    else:
        active_ids = _read_active_server_ids("config.ini")

    peer_ids = [sid for sid in active_ids if sid != server_id]
    port = 9001 + server_id

    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    raft_pb2_grpc.add_KeyValueStoreServicer_to_server(
        KeyValueStoreServicer(server_id, peer_ids, base_port=9001), grpc_server
    )
    grpc_server.add_insecure_port(f"[::]:{port}")
    grpc_server.start()
    grpc_server.wait_for_termination()
