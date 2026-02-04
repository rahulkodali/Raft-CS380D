from concurrent import futures
import subprocess
import raft_pb2, raft_pb2_grpc
import grpc
import sys

class FrontEndServicer(raft_pb2_grpc.FrontEndServicer):
    def __init__(self):
        self.ports = set()

    def findStub(self):
        for port in self.ports:
            channel = grpc.insecure_channel(f"localhost:{port}")
            stub = raft_pb2_grpc.KeyValueStoreStub(channel)
            resp = stub.ping(raft_pb2.Empty(), timeout=0.2)
            if resp.success:
                return stub
        return None
    
    def Get(self, request, context):
        stub = self.findStub()
        if stub is None:
            return raft_pb2.Reply(wrongLeader=True, error="no servers available")
        kv = stub.Get(raft_pb2.StringArg(arg=request.key))
        return raft_pb2.Reply(wrongLeader=False, value=kv.value)

    def Put(self, request, context):
        stub = self.findStub()
        if stub is None:
            return raft_pb2.Reply(wrongLeader=True, error="no servers available")
        resp = stub.Put(request)
        if resp.success:
            return raft_pb2.Reply(wrongLeader=False)
        return raft_pb2.Reply(wrongLeader=True, error=resp.error)

    
    def StartRaft(self, request, context):
        try:
            num_servers = request.arg
            for i in range(num_servers):
                subprocess.Popen(["python3", "server.py", str(i)])
                self.ports.add(9001 + i)
            return raft_pb2.Reply(wrongLeader=False)
        except Exception as e:
            return raft_pb2.Reply(wrongLeader=True, error = str(e))

    def StartServer(self, request, context):
        try:
            server_number = request.arg
            subprocess.Popen(["python3", "server.py", str(server_number)])
            self.ports.add(9001 + server_number)
            return raft_pb2.Reply(wrongLeader=False)
        except Exception as e:
            return raft_pb2.Reply(wrongLeader=True, error = str(e))
 

if __name__ == "__main__":
    port = 8001
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    raft_pb2_grpc.add_FrontEndServicer_to_server(FrontEndServicer(), grpc_server)
    grpc_server.add_insecure_port(f"[::]:{port}")
    grpc_server.start()
    grpc_server.wait_for_termination()

# Start server on port 8001