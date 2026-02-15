import configparser
import random
import sys
import threading
import time
from concurrent import futures

import grpc

import raft_pb2
import raft_pb2_grpc


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

    ##reset when to start election
    def _reset_election_deadline(self):
        self.election_deadline = time.time() + self.rng.uniform(0.150, 0.300)

    ##follower helper
    def _become_follower(self, new_term, leader_id=None):
        self.currentTerm = new_term
        self.role = "follower"
        self.leaderId = leader_id
        self.votedFor = None
        self._reset_election_deadline()

    ##leader helper
    def _become_leader(self):
        self.role = "leader"
        self.leaderId = self.server_id


    def _cluster_majority(self):
        return len(self.peer_ids) + 1

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

    ##follower election loop
    def _run_election_loop(self):
        while not self.stop_event.is_set():
            time.sleep(0.02)

            with self.state_lock:
                if self.role == "leader" or time.time() < self.election_deadline:
                    continue

                # Start election (node votes for itself)
                self.role = "candidate"
                self.currentTerm += 1
                election_term = self.currentTerm
                self.votedFor = self.server_id
                self.leaderId = None
                votes = 1
                self._reset_election_deadline()

            vote_reqs = []
            ##send vote args to all peers and store in array
            ##spawn a thread for each _request_vote
            with futures.ThreadPoolExecutor(max_workers=max(1, len(self.peer_ids))) as pool:
                for peer in self.peer_ids:
                    vote_reqs.append(pool.submit(self._request_vote, peer, election_term))

                ##go through all vote replies
                for req in futures.as_completed(vote_reqs):
                    try:
                        reply = req.result()
                    except Exception:
                        continue
                    
                    if reply is None:
                        continue
                    ##updates
                    with self.state_lock:
                        ##if state has changed break early
                        if self.role != "candidate" or self.currentTerm != election_term:
                            break
                        ##early break if not possible
                        if reply.term > self.currentTerm:
                            self._become_follower(reply.term)
                            break
                        ##vote checking
                        if reply.voteGranted:
                            votes += 1
                            if votes >= self._cluster_majority():
                                self._become_leader()
                                break

    ##leader heartbeat                         
    def _run_heartbeat_loop(self):
        while not self.stop_event.is_set():
            time.sleep(0.05)
            
            with self.state_lock:
                if self.role != "leader":
                    continue
                heartbeat_term = self.currentTerm

            ##sends append entries heartbeat to all peers
            for peer in self.peer_ids:
                try:
                    channel = grpc.insecure_channel(f"localhost:{self.base_port + peer}")
                    stub = raft_pb2_grpc.KeyValueStoreStub(channel)
                    reply = stub.AppendEntries(
                        raft_pb2.AppendEntriesArgs(
                            term=heartbeat_term,
                            leaderId=self.server_id,
                            prevLogIndex=0,
                            prevLogTerm=0,
                            entries=[],
                            leaderCommit=0,
                        ),
                        timeout=0.3,
                    )
                    with self.state_lock:
                        ##if we get a larger term response step down
                        if reply.term > self.currentTerm:
                            self._become_follower(reply.term)
                            break
                except Exception:
                    continue

    def ping(self, request, context):
        return raft_pb2.GenericResponse(success=True)

    def GetState(self, request, context):
        with self.state_lock:
            return raft_pb2.State(term=self.currentTerm, isLeader=(self.role == "leader"))

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

            if request.term >= self.currentTerm or self.role != "follower":
                self._become_follower(request.term, leader_id=request.leaderId)
            else:
                self.leaderId = request.leaderId
                self._reset_election_deadline()

            return raft_pb2.AppendEntriesReply(term=self.currentTerm, success=True)

    def Get(self, request, context):
        key = request.arg
        with self.store_lock:
            value = self.storage.get(key, "")
        return raft_pb2.KeyValue(key=key, value=value)

    def Put(self, request, context):
        with self.store_lock:
            self.storage[request.key] = request.value
        return raft_pb2.GenericResponse(success=True)


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
