import configparser
import os
import shutil
import subprocess
import sys
from concurrent import futures

import grpc

import raft_pb2
import raft_pb2_grpc


class FrontEndServicer(raft_pb2_grpc.FrontEndServicer):
    def __init__(self, config_path="config.ini", base_port=9001):
        self.config_path = config_path
        self.base_port = base_port
        self.cluster_size = len(self._active_server_ids())
        self.server_processes = {}

    def _active_server_ids(self):
        parser = configparser.ConfigParser()
        if parser.read(self.config_path):
            try:
                active = parser.get("Servers", "active")
                ids = [int(x.strip()) for x in active.split(",") if x.strip()]
                if ids:
                    return ids
            except Exception:
                pass
        return [0, 1, 2, 3, 4]

    def _persistent_state_path(self):
        parser = configparser.ConfigParser()
        parser.read(self.config_path)
        return parser.get("Servers", "persistent_state_path", fallback="memory")

    def _get_stub(self, server_id):
        channel = grpc.insecure_channel(f"localhost:{self.base_port + server_id}")
        return raft_pb2_grpc.KeyValueStoreStub(channel)

    def _find_leader_stub(self):
        fallback = None
        for sid in self._active_server_ids():
            try:
                stub = self._get_stub(sid)
                ping = stub.ping(raft_pb2.Empty(), timeout=0.2)
                if not ping.success:
                    continue
                if fallback is None:
                    fallback = stub
                state = stub.GetState(raft_pb2.Empty(), timeout=0.2)
                if state.isLeader:
                    return stub
            except Exception:
                continue
        return fallback

    def _stop_server_process(self, server_id):
        proc = self.server_processes.get(server_id)
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        self.server_processes.pop(server_id, None)

    def _stop_all_server_processes(self):
        for sid in list(self.server_processes.keys()):
            self._stop_server_process(sid)

    def Get(self, request, context):
        try:
            stub = self._find_leader_stub()
            if stub is None:
                return raft_pb2.Reply(wrongLeader=True, error="No leader available")
            kv = stub.Get(raft_pb2.StringArg(arg=request.key), timeout=0.5)
            return raft_pb2.Reply(wrongLeader=False, value=kv.value)
        except Exception as exc:
            return raft_pb2.Reply(wrongLeader=True, error=str(exc))

    def Put(self, request, context):
        try:
            stub = self._find_leader_stub()
            if stub is None:
                return raft_pb2.Reply(wrongLeader=True, error="No leader available")
            resp = stub.Put(request, timeout=0.5)
            if resp.success:
                return raft_pb2.Reply(wrongLeader=False)
            return raft_pb2.Reply(wrongLeader=True, error=resp.error or "Put failed")
        except Exception as exc:
            return raft_pb2.Reply(wrongLeader=True, error=str(exc))

    def StartRaft(self, request, context):
        try:
            num_servers = request.arg
            self.cluster_size = num_servers
            self._stop_all_server_processes()

            # Assignment 6: clean state directory for new cluster start.
            state_path = self._persistent_state_path()
            if state_path != "memory":
                if os.path.exists(state_path):
                    shutil.rmtree(state_path)
                os.makedirs(state_path, exist_ok=True)

            for i in range(num_servers):
                proc = subprocess.Popen([sys.executable, "server.py", str(i), str(num_servers)])
                self.server_processes[i] = proc
            return raft_pb2.Reply(wrongLeader=False)
        except Exception as exc:
            return raft_pb2.Reply(wrongLeader=True, error=str(exc))

    def StartServer(self, request, context):
        try:
            server_number = request.arg
            self._stop_server_process(server_number)
            proc = subprocess.Popen(
                [sys.executable, "server.py", str(server_number), str(self.cluster_size)]
            )
            self.server_processes[server_number] = proc
            return raft_pb2.Reply(wrongLeader=False)
        except Exception as exc:
            return raft_pb2.Reply(wrongLeader=True, error=str(exc))

if __name__ == "__main__":
    port = 8001
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    raft_pb2_grpc.add_FrontEndServicer_to_server(FrontEndServicer(), grpc_server)
    grpc_server.add_insecure_port(f"127.0.0.1:{port}")
    grpc_server.start()
    grpc_server.wait_for_termination()
