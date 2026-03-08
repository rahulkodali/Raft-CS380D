import configparser
import json
import os
import random
import sys
import threading
import time
from concurrent import futures

import grpc

import raft_pb2
import raft_pb2_grpc


class LogEntry:
    def __init__(self, term, key, value, client_id=-1, request_id=-1):
        self.term = term
        self.key = key
        self.value = value
        self.clientId = client_id
        self.requestId = request_id


class KeyValueStoreServicer(raft_pb2_grpc.KeyValueStoreServicer):
    def __init__(self, server_id, peer_ids, base_port=9001, persistent_state_path="memory"):
        self.server_id = server_id
        self.peer_ids = peer_ids
        self.base_port = base_port

        # Assignment 2 state machine
        self.storage = {}
        self.store_lock = threading.RLock()

        # Raft state
        self.state_lock = threading.RLock()
        self.currentTerm = 0
        self.votedFor = None
        self.role = "follower"
        self.leaderId = None

        # replication state
        self.log = [None]  # index 0 sentinel
        self.commit_index = 0
        self.last_applied = 0
        self.match_index = {peer_id: 0 for peer_id in peer_ids}
        self.next_index = {peer_id: 1 for peer_id in peer_ids}
        self.apply_cv = threading.Condition(self.state_lock)

        # persistence config
        self.persistent_state_path = persistent_state_path
        self.persistence_enabled = self.persistent_state_path != "memory"
        self.state_file = None
        if self.persistence_enabled:
            self.state_file = os.path.join(self.persistent_state_path, f"server_{self.server_id}.json")
            os.makedirs(self.persistent_state_path, exist_ok=True)

        # Load persistent state before worker threads start.
        self._load_state()

        self.stop_event = threading.Event()
        self.election_deadline = 0.0
        self.rng = random.Random(time.time_ns() ^ (server_id << 16))
        self._reset_election_deadline()

        self.election_thread = threading.Thread(target=self._run_election_loop, daemon=True)
        self.heartbeat_thread = threading.Thread(target=self._run_heartbeat_loop, daemon=True)
        self.election_thread.start()
        self.heartbeat_thread.start()

    def _persist_state_locked(self):
        if not self.persistence_enabled:
            return

        payload = {
            "currentTerm": self.currentTerm,
            "votedFor": self.votedFor,
            "log": [
                {
                    "term": entry.term,
                    "key": entry.key,
                    "value": entry.value,
                    "clientId": getattr(entry, "clientId", -1),
                    "requestId": getattr(entry, "requestId", -1),
                }
                for entry in self.log[1:]
            ],
        }

        tmp_file = f"{self.state_file}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, self.state_file)

        # Best effort directory fsync for rename durability.
        try:
            dir_fd = os.open(self.persistent_state_path, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass

    def _load_state(self):
        if not self.persistence_enabled:
            return
        if not os.path.exists(self.state_file):
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return

        self.currentTerm = int(payload.get("currentTerm", 0))
        self.votedFor = payload.get("votedFor", None)

        loaded_log = [None]
        for item in payload.get("log", []):
            loaded_log.append(
                LogEntry(
                    term=int(item.get("term", 0)),
                    key=item.get("key", ""),
                    value=item.get("value", ""),
                    client_id=int(item.get("clientId", -1)),
                    request_id=int(item.get("requestId", -1)),
                )
            )
        self.log = loaded_log

        # Volatile state rebuilt after restart.
        self.commit_index = 0
        self.last_applied = 0
        self.role = "follower"
        self.leaderId = None
        self.storage = {}

    def _reset_election_deadline(self):
        self.election_deadline = time.time() + self.rng.uniform(0.150, 0.300)

    def _become_follower(self, new_term, leader_id=None):
        self.currentTerm = new_term
        self.role = "follower"
        self.leaderId = leader_id
        self.votedFor = None
        self._persist_state_locked()
        self._reset_election_deadline()
        self.apply_cv.notify_all()

    def _become_leader(self):
        self.role = "leader"
        self.leaderId = self.server_id
        next_idx = len(self.log)
        for peer in self.peer_ids:
            self.next_index[peer] = next_idx
            self.match_index[peer] = 0

        # no-op entry on leadership to allow committing prior-term entries.
        noop = LogEntry(self.currentTerm, "", "", -1, -1)
        self.log.append(noop)
        self._persist_state_locked()

    def _apply_committed_entries(self):
        with self.store_lock:
            while self.last_applied < self.commit_index:
                self.last_applied += 1
                entry = self.log[self.last_applied]
                # Ignore no-op entry in state machine.
                if entry.key == "" and entry.value == "" and entry.clientId == -1 and entry.requestId == -1:
                    continue
                self.storage[entry.key] = entry.value

    def _advance_commit_index(self):
        for i in range(len(self.log) - 1, self.commit_index, -1):
            if self.log[i].term != self.currentTerm:
                continue
            count = 1  # leader
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

            last_log_index = len(self.log) - 1
            last_log_term = self.log[last_log_index].term if last_log_index > 0 else 0
            return stub.RequestVote(
                raft_pb2.RequestVoteArgs(
                    term=election_term,
                    candidateId=self.server_id,
                    lastLogIndex=last_log_index,
                    lastLogTerm=last_log_term,
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
                self._persist_state_locked()
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

                        entries = [
                            raft_pb2.LogEntry(
                                term=e.term,
                                key=e.key,
                                value=e.value,
                                clientId=e.clientId,
                                requestId=e.requestId,
                            )
                            for e in self.log[self.next_index[peer] :]
                        ]
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
                self._persist_state_locked()
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
            changed = False
            idx = request.prevLogIndex + 1
            for entry in request.entries:
                incoming = LogEntry(entry.term, entry.key, entry.value, entry.clientId, entry.requestId)
                if idx < len(self.log):
                    if self.log[idx].term != incoming.term:
                        self.log = self.log[:idx]
                        self.log.append(incoming)
                        changed = True
                else:
                    self.log.append(incoming)
                    changed = True
                idx += 1

            if changed:
                self._persist_state_locked()

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

            log_entry = LogEntry(
                term=self.currentTerm,
                key=request.key,
                value=request.value,
                client_id=request.clientId,
                request_id=request.requestId,
            )
            self.log.append(log_entry)
            self._persist_state_locked()
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

    parser = configparser.ConfigParser()
    parser.read("config.ini")
    persistent_state_path = parser.get("Servers", "persistent_state_path", fallback="memory")

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
        KeyValueStoreServicer(
            server_id,
            peer_ids,
            base_port=9001,
            persistent_state_path=persistent_state_path,
        ),
        grpc_server,
    )
    grpc_server.add_insecure_port(f"[::]:{port}")
    grpc_server.start()
    grpc_server.wait_for_termination()
