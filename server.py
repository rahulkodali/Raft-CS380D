import sys
import grpc
import raft_pb2, raft_pb2_grpc
from concurrent import futures

class KeyValueStoreServicer(raft_pb2_grpc.KeyValueStoreServicer):
    def ping(self, request, context):
        return raft_pb2.GenericResponse(success=True)
    
    def GetState(self, request, context):
        return raft_pb2.State(term=0, isLeader=False)

if __name__ == '__main__':
    server_id = int(sys.argv[1])
    port = 9001 + server_id

    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    raft_pb2_grpc.add_KeyValueStoreServicer_to_server(KeyValueStoreServicer(), grpc_server)
    grpc_server.add_insecure_port(f'[::]:{port}')
    grpc_server.start()
    print("server:" + str(port))
    grpc_server.wait_for_termination()
