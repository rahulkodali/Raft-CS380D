from concurrent import futures
import subprocess
import raft_pb2, raft_pb2_grpc
import grpc
import sys

class FrontEndServicer(raft_pb2_grpc.FrontEndServicer):
    def StartRaft(self, request, context):

        num_servers = request.arg
        for i in range(num_servers):
            subprocess.Popen([sys.executable, "server.py", str(i)])
        return raft_pb2.Reply(wrongLeader=False)

    def StartServer(self, request, context):
        server_number = request.arg
        subprocess.Popen([sys.executable, "server.py", str(server_number)])
        return raft_pb2.Reply(wrongLeader=False)
    
    def Get(self, request, context):
        return raft_pb2.Reply(wrongLeader=True, error="Not implemented")

    def Put(self, request, context):
        return raft_pb2.Reply(wrongLeader=True, error="Not implemented")
 

if __name__ == "__main__":
    port = 8001
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    raft_pb2_grpc.add_FrontEndServicer_to_server(FrontEndServicer(), grpc_server)
    grpc_server.add_insecure_port(f"[::]:{port}")
    grpc_server.start()
    grpc_server.wait_for_termination()

# Start server on port 8001