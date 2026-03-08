#!/usr/bin/env python3
"""
Assignment 5 Language-Agnostic Test Script (Updated Nov 12, 2025)
Tests fault tolerance, leader failure, and recovery
"""

import subprocess
import time
import grpc
import sys
import os
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
import signal
import atexit
import json
import random
import string
import threading
from datetime import datetime

# Import the generated protobuf files
try:
    import raft_pb2
    import raft_pb2_grpc
except ImportError:
    print("ERROR: Could not import raft_pb2 or raft_pb2_grpc")
    print("Please generate them from raft.proto first:")
    print("  python -m grpc_tools.protoc --python_out=. --grpc_python_out=. raft.proto")
    sys.exit(1)

# Global variables to track processes
frontend_process = None
server_processes = {}

class ExecutionConfig:
    """Configuration for how to start frontend and server processes"""
    
    def __init__(self, config_file="test_config.json"):
        self.config = self.load_config(config_file)
        self.validate_config()
    
    def load_config(self, config_file):
        """Load execution configuration"""
        # Default configuration
        default_config = {
            "frontend": {
                "command": [sys.executable, os.path.join(PROJECT_ROOT, "frontend.py")],
                "working_dir": PROJECT_ROOT,
                "env": {"PYTHONUNBUFFERED": "1"},
                "process_name_pattern": "frontend.py",
                "startup_timeout": 30
            },
            "server": {
                "command_template": [sys.executable, os.path.join(PROJECT_ROOT, "server.py"), "{server_id}"],
                "working_dir": PROJECT_ROOT,
                "env": {"PYTHONUNBUFFERED": "1"},
                "process_name_pattern": "server.py",
                "startup_timeout": 10
            },
            "ports": {
                "frontend_port": 8001,
                "base_server_port": 9001
            },
            "timeouts": {
                "rpc_timeout": 5,
                "startup_wait": 4,
                "server_ready_timeout": 20,
                "leader_election_timeout": 15,
                "replication_timeout": 10,
                "commit_timeout": 10,
                "recovery_timeout": 20,      # Time for server to catch up
                "failover_timeout": 30        # Time for new leader election after failure
            }
        }
        
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    user_config = json.load(f)
                self.merge_config(default_config, user_config)
                print(f"Loaded configuration from {config_file}")
            except Exception as e:
                print(f"Error loading {config_file}: {e}")
                print("Using default configuration")
        else:
            print(f"Configuration file {config_file} not found, using defaults")
        
        return default_config
    
    def merge_config(self, default, user):
        """Deep merge user config into default config"""
        for key, value in user.items():
            if key in default and isinstance(default[key], dict) and isinstance(value, dict):
                self.merge_config(default[key], value)
            else:
                default[key] = value
    
    def validate_config(self):
        """Validate configuration structure"""
        required_sections = ['frontend', 'server', 'ports', 'timeouts']
        for section in required_sections:
            if section not in self.config:
                raise ValueError(f"Missing required config section: {section}")
    
    def get_frontend_command(self):
        """Get command to start frontend"""
        return self.config['frontend']['command']
    
    def get_server_command(self, server_id):
        """Get command to start a specific server"""
        template = self.config['server']['command_template']
        return [cmd.format(server_id=server_id) for cmd in template]
    
    def get_working_dir(self, service_type):
        """Get working directory for service"""
        return self.config[service_type].get('working_dir', '.')
    
    def get_env(self, service_type):
        """Get environment variables for service"""
        env = os.environ.copy()
        service_env = self.config[service_type].get('env', {})
        env.update(service_env)
        return env

class TestResult:
    def __init__(self, name, score, max_points, details):
        self.name = name
        self.score = score
        self.max_points = max_points
        self.details = details

class TestSuite:
    def __init__(self):
        self.results = []
        self.total = 0

    def add(self, result):
        self.results.append(result)
        self.total += result.score

    def print_results(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n===== ASSIGNMENT 5 TEST RESULTS ({now}) =====")
        
        passed = 0
        for r in self.results:
            if r.score == r.max_points:
                status = "PASS"
                passed += 1
            elif r.score > 0:
                status = "PART"
            else:
                status = "FAIL"
            
            print(f"{r.name:<45} [{status}] {r.score:.1f}/{r.max_points:.1f} — {r.details}")
        
        total_tests = len(self.results)
        failed = total_tests - passed
        pass_rate = (passed / total_tests * 100) if total_tests > 0 else 0
        
        print(f"\nTests run: {total_tests}, Passed: {passed}, Failed: {failed} ({pass_rate:.1f}% pass rate)")
        print(f"Overall Score: {self.total:.1f}/100")
        print("=" * 80)

# Initialize configuration
config = None

def init_config():
    """Initialize global configuration"""
    global config
    config = ExecutionConfig()

def cleanup_all():
    """Clean up all processes on exit"""
    global frontend_process, server_processes
    
    print("\nCleaning up all processes...")
    
    # Kill frontend process
    if frontend_process and frontend_process.poll() is None:
        try:
            frontend_process.terminate()
            frontend_process.wait(timeout=5)
            print("Frontend process terminated")
        except subprocess.TimeoutExpired:
            frontend_process.kill()
            frontend_process.wait()
            print("Frontend process killed")
        except:
            pass
    
    # Kill server processes
    for server_id, process in server_processes.items():
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
                print(f"Server {server_id} terminated")
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                print(f"Server {server_id} killed")
            except:
                pass
    
    server_processes.clear()
    cleanup_all_processes()

def cleanup_all_processes():
    """Kill all processes including frontend (for final cleanup)"""
    if not config:
        return
    
    patterns = []
    
    # Add frontend pattern
    if 'process_name_pattern' in config.config['frontend']:
        patterns.append(config.config['frontend']['process_name_pattern'])
    
    # Add server pattern
    if 'process_name_pattern' in config.config['server']:
        patterns.append(config.config['server']['process_name_pattern'])
    
    for pattern in patterns:
        if pattern:
            try:
                subprocess.run(['pkill', '-f', pattern], 
                             capture_output=True, timeout=5)
            except:
                pass
    
    time.sleep(2)

def start_frontend():
    """Start the frontend service"""
    global frontend_process

    print("Starting frontend service...")
    import socket

    def probe():
        try:
            with socket.create_connection(("127.0.0.1", config.config['ports']['frontend_port']), timeout=0.5):
                return True
        except Exception:
            return False

    if probe():
        print("Frontend already running; reusing existing instance.")
        return True

    cmd = config.get_frontend_command()
    working_dir = config.get_working_dir('frontend')
    env = config.get_env('frontend')

    frontend_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=working_dir,
        env=env,
        preexec_fn=os.setsid,
        text=True,
        bufsize=1
    )

    deadline = time.time() + config.config['frontend']['startup_timeout']
    while time.time() < deadline:
        if probe():
            print("Frontend service ready.")
            return True

        rc = frontend_process.poll()
        if rc is not None:
            print("\nERROR: Frontend exited early")
            return False

        time.sleep(0.5)

    print("\nERROR: Frontend service failed to start in time.")
    return False

def call_start_raft(n):
    """Call StartRaft RPC with n servers"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        
        channel = grpc.insecure_channel(f'127.0.0.1:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.IntegerArg(arg=n)
        response = stub.StartRaft(request, timeout=30)
        channel.close()
        
        if response.error:
            return False, response.error
        return True, ""
    except Exception as e:
        return False, str(e)

def call_start_server(server_id):
    """Call StartServer RPC to restart a specific server"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        
        channel = grpc.insecure_channel(f'127.0.0.1:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.IntegerArg(arg=server_id)
        response = stub.StartServer(request, timeout=15)
        channel.close()
        
        if response.error:
            return False, response.error
        return True, ""
    except Exception as e:
        return False, str(e)

def kill_server(server_id):
    """Kill a specific server process
    """
    try:
        base_port = config.config['ports']['base_server_port']
        port = base_port + server_id
        
        # Find and kill process listening on the port
        # -n: no hostname resolution, -P: no port name resolution
        # -iTCP:{port}: TCP on specific port, -sTCP:LISTEN: only listening state
        # -t: terse output (PID only)
        result = subprocess.run(
            ['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.stdout.strip():
            # Get all PIDs (in case there are multiple)
            pids = result.stdout.strip().split('\n')
            for pid_str in pids:
                try:
                    pid = int(pid_str)
                    os.kill(pid, signal.SIGKILL)
                    print(f"Killed server {server_id} (PID {pid})")
                except (ValueError, ProcessLookupError):
                    continue
            time.sleep(0.5)
            return True
        else:
            print(f"No process found listening on port {port}")
            return False
    except Exception as e:
        print(f"Error killing server {server_id}: {e}")
        return False

def ping_server(server_id):
    """Ping a specific server"""
    try:
        base_port = config.config['ports']['base_server_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        addr = f"127.0.0.1:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.Empty()
        response = stub.ping(request, timeout=rpc_timeout)
        channel.close()
        
        return response.success
    except:
        return False

def get_server_state(server_id):
    """Get state from a specific server"""
    try:
        base_port = config.config['ports']['base_server_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        addr = f"127.0.0.1:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.Empty()
        response = stub.GetState(request, timeout=rpc_timeout)
        channel.close()
        
        return True, response.term, response.isLeader, response.commitIndex, response.lastApplied
    except Exception as e:
        return False, 0, False, 0, 0

def server_put(server_id, key, value):
    """Call Put directly on server"""
    try:
        base_port = config.config['ports']['base_server_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        addr = f"127.0.0.1:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.KeyValue(key=key, value=value, clientId=1, requestId=random.randint(1, 100000))
        response = stub.Put(request, timeout=rpc_timeout)
        channel.close()
        
        return response.success
    except Exception as e:
        return False

def server_get(server_id, key):
    """Call Get directly on server"""
    try:
        base_port = config.config['ports']['base_server_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        addr = f"127.0.0.1:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.StringArg(arg=key)
        response = stub.Get(request, timeout=rpc_timeout)
        channel.close()
        
        return True, response.key, response.value
    except Exception as e:
        return False, "", str(e)

def frontend_put(key, value):
    """Call Put on frontend"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        channel = grpc.insecure_channel(f'127.0.0.1:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.KeyValue(key=key, value=value, clientId=1, requestId=random.randint(1, 100000))
        response = stub.Put(request, timeout=rpc_timeout)
        channel.close()
        
        if response.wrongLeader:
            return False, response.error
        return True, ""
    except Exception as e:
        return False, str(e)

def frontend_get(key):
    """Call Get on frontend"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        channel = grpc.insecure_channel(f'127.0.0.1:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.GetKey(key=key, clientId=1, requestId=1)
        response = stub.Get(request, timeout=rpc_timeout)
        channel.close()
        
        if response.wrongLeader:
            return False, "", response.error
        return True, response.value, ""
    except Exception as e:
        return False, "", str(e)

def wait_for_leader_election(num_servers, timeout_seconds=15, active_servers=None):
    """Wait for leader election to complete and return leader info
    
    Args:
        num_servers: Total number of servers in cluster
        timeout_seconds: How long to wait
        active_servers: List of server IDs to check (if None, checks all)
    """
    if active_servers is None:
        active_servers = list(range(num_servers))
    
    print(f"Waiting up to {timeout_seconds}s for leader election among servers {active_servers}...")
    
    start_time = time.time()
    
    while time.time() - start_time < timeout_seconds:
        leaders = []
        server_states = {}
        
        for server_id in active_servers:
            success, term, is_leader, commit_idx, last_applied = get_server_state(server_id)
            if success:
                server_states[server_id] = {
                    'term': term, 
                    'is_leader': is_leader,
                    'commitIndex': commit_idx,
                    'lastApplied': last_applied
                }
                if is_leader:
                    leaders.append(server_id)
        
        if len(leaders) == 1:
            leader_id = leaders[0]
            leader_term = server_states[leader_id]['term']
            print(f"Leader elected: Server {leader_id} in term {leader_term}")
            return True, leader_id, leader_term, server_states
        elif len(leaders) > 1:
            print(f"WARNING: Multiple leaders detected: {leaders}")
        
        time.sleep(1)
    
    print(f"Leader election timed out after {timeout_seconds}s")
    return False, None, None, {}

def wait_for_commit(active_servers, expected_commit_index, timeout_seconds=10):
    """Wait for active servers to reach expected commit index"""
    print(f"Waiting for servers {active_servers} to reach commit index {expected_commit_index}...")
    
    start_time = time.time()
    
    while time.time() - start_time < timeout_seconds:
        all_committed = True
        commit_indices = {}
        
        for server_id in active_servers:
            success, term, is_leader, commit_idx, last_applied = get_server_state(server_id)
            if success:
                commit_indices[server_id] = commit_idx
                if commit_idx < expected_commit_index:
                    all_committed = False
            else:
                all_committed = False
        
        if all_committed:
            print(f"All active servers reached commit index {expected_commit_index}")
            return True, commit_indices
        
        time.sleep(0.5)
    
    print(f"Timeout waiting for commit. Final indices: {commit_indices}")
    return False, commit_indices

def generate_test_data(n=5):
    """Generate random test data"""
    keys = []
    values = []
    
    for i in range(n):
        key = ''.join(random.choices(string.ascii_lowercase, k=5)) + str(i)
        value = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
        keys.append(key)
        values.append(value)
    
    return keys, values

# Test functions for Assignment 5

def test_basic_cluster_startup():
    """Test 1: Basic Cluster Startup"""
    print("\n=== Test: Basic Cluster Startup ===")

    test_name = "Basic Startup"
    test_max_points = 0

    # Start frontend
    if not start_frontend():
        return TestResult(
            test_name, 0, test_max_points, "Frontend failed to start"
        )

    # Start 5-server cluster
    print("Starting 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"StartRaft(5) failed: {error}"
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Wait for leader election
    success, leader_id, leader_term, server_states = wait_for_leader_election(5)
    
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader elected"
        )

    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"5-server cluster started, leader: Server {leader_id}",
    )

def test_leader_failure_detection():
    """Test 2: Leader Failure Detection (15 points)"""
    print("\n=== Test: Leader Failure Detection ===")

    test_name = "Leader Failure Detection"
    test_max_points = 15

    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )
    
    time.sleep(config.config["timeouts"]["startup_wait"])

    # Find current leader
    success, leader_id, leader_term, server_states = wait_for_leader_election(5, timeout_seconds=10)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    print(f"Current leader: Server {leader_id} (term {leader_term})")

    # Kill the leader
    print(f"Killing leader (Server {leader_id})...")
    if not kill_server(leader_id):
        return TestResult(
            test_name, 5, test_max_points, "Failed to kill leader"
        )

    time.sleep(1)

    # Wait for new leader election
    active_servers = [i for i in range(5) if i != leader_id]
    success, new_leader_id, new_term, new_states = wait_for_leader_election(
        5, 
        timeout_seconds=config.config["timeouts"]["failover_timeout"],
        active_servers=active_servers
    )

    if not success:
        return TestResult(
            test_name, 7, test_max_points, "No new leader elected after failure"
        )

    if new_term <= leader_term:
        return TestResult(
            test_name, 10, test_max_points, 
            f"New leader has same/lower term ({new_term} vs {leader_term})"
        )

    print(f"New leader elected: Server {new_leader_id} (term {new_term})")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"New leader (Server {new_leader_id}) elected in higher term ({new_term})",
    )

def test_operations_after_leader_failure():
    """Test 3: Operations Continue After Leader Failure (15 points)"""
    print("\n=== Test: Operations After Leader Failure ===")

    test_name = "Ops After Failure"
    test_max_points = 15

    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )
    
    time.sleep(config.config["timeouts"]["startup_wait"])

    # Find current leader
    success, leader_id, leader_term, _ = wait_for_leader_election(5, timeout_seconds=10)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Write some data before failure
    test_key = "before_failure"
    test_value = "data_before"
    print(f"Writing data before failure: {test_key} -> {test_value}")
    if not server_put(leader_id, test_key, test_value):
        return TestResult(
            test_name, 3, test_max_points, "Failed to write data before failure"
        )

    time.sleep(2)  # Let it replicate

    # Kill the leader
    print(f"Killing leader (Server {leader_id})...")
    kill_server(leader_id)
    time.sleep(1)

    # Wait for new leader
    active_servers = [i for i in range(5) if i != leader_id]
    success, new_leader_id, new_term, _ = wait_for_leader_election(
        5,
        timeout_seconds=config.config["timeouts"]["failover_timeout"],
        active_servers=active_servers
    )

    if not success:
        return TestResult(
            test_name, 5, test_max_points, "No new leader elected"
        )

    # Try to write new data with new leader
    new_key = "after_failure"
    new_value = "data_after"
    print(f"Writing data with new leader: {new_key} -> {new_value}")
    if not server_put(new_leader_id, new_key, new_value):
        return TestResult(
            test_name, 10, test_max_points, "Failed to write data with new leader"
        )

    time.sleep(2)

    # Verify both old and new data are accessible
    success1, _, val1 = server_get(new_leader_id, test_key)
    success2, _, val2 = server_get(new_leader_id, new_key)

    if not success1 or val1 != test_value:
        return TestResult(
            test_name, 12, test_max_points, 
            f"Old data lost or corrupted: expected '{test_value}', got '{val1}'"
        )

    if not success2 or val2 != new_value:
        return TestResult(
            test_name, 12, test_max_points, 
            f"New data not written: expected '{new_value}', got '{val2}'"
        )

    print(f"Both old and new data accessible on new leader")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Operations continue successfully with new leader",
    )

def test_minority_failure_tolerance():
    """Test 4: Cluster Operates with Minority Failures (15 points)"""
    print("\n=== Test: Minority Failure Tolerance ===")

    test_name = "Minority Failure"
    test_max_points = 15

    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )
    
    time.sleep(config.config["timeouts"]["startup_wait"])

    # Find current leader
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=10)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Kill 2 non-leader servers (minority in 5-server cluster)
    non_leaders = [i for i in range(5) if i != leader_id]
    servers_to_kill = non_leaders[:2]

    print(f"Killing minority servers: {servers_to_kill}")
    for server_id in servers_to_kill:
        kill_server(server_id)
    
    time.sleep(2)

    # Verify cluster still has a leader
    active_servers = [i for i in range(5) if i not in servers_to_kill]
    success, current_leader, _, _ = wait_for_leader_election(
        5,
        timeout_seconds=5,
        active_servers=active_servers
    )

    if not success:
        return TestResult(
            test_name, 5, test_max_points, "No leader after minority failure"
        )

    # Try to perform operations
    test_key = "minority_failure_test"
    test_value = "still_working"
    print(f"Writing data with minority down: {test_key} -> {test_value}")
    
    if not server_put(current_leader, test_key, test_value):
        return TestResult(
            test_name, 10, test_max_points, "Cannot write data with minority down"
        )

    # Wait longer for replication to complete (minority means slower replication)
    time.sleep(4)

    # Verify data can be read from remaining servers
    consistent_count = 0
    for server_id in active_servers:
        success, _, val = server_get(server_id, test_key)
        if success and val == test_value:
            consistent_count += 1

    if consistent_count == len(active_servers):
        print(f"Data replicated to all {len(active_servers)} active servers")
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            f"Cluster operates correctly with {len(servers_to_kill)}/5 servers down",
        )
    else:
        return TestResult(
            test_name, 12, test_max_points, 
            f"Only {consistent_count}/{len(active_servers)} servers have consistent data"
        )

def test_server_recovery_and_catchup():
    """Test 5: Server Recovery and Catch-up (10 points)"""
    print("\n=== Test: Server Recovery and Catch-up ===")

    test_name = "Recovery & Catch-up"
    test_max_points = 10

    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )
    
    time.sleep(config.config["timeouts"]["startup_wait"])

    # Find current leader
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=10)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Pick a non-leader server to kill
    non_leaders = [i for i in range(5) if i != leader_id]
    failed_server = non_leaders[0]

    print(f"Killing server {failed_server}")
    kill_server(failed_server)
    time.sleep(3)

    # Write data while server is down
    test_data = {}
    print(f"Writing data while server {failed_server} is down...")
    for i in range(2):
        key = f"recovery_key_{i}"
        value = f"recovery_value_{i}"
        test_data[key] = value
        if not server_put(leader_id, key, value):
            return TestResult(
                test_name, 5, test_max_points, f"Failed to write data (key {i})"
            )
        time.sleep(0.5)

    # Restart the failed server
    print(f"Restarting server {failed_server}...")
    success, error = call_start_server(failed_server)
    if not success:
        return TestResult(
            test_name, 7, test_max_points, f"Failed to restart server: {error}"
        )

    # Wait for server to catch up
    print(f"Waiting for server {failed_server} to catch up...")
    time.sleep(config.config["timeouts"]["recovery_timeout"])

    # Verify recovered server has all the data
    print(f"Verifying data on recovered server {failed_server}...")
    caught_up = True
    for key, expected_value in test_data.items():
        success, _, value = server_get(failed_server, key)
        if not success or value != expected_value:
            print(f"  {key}: expected '{expected_value}', got '{value}'")
            caught_up = False
        else:
            print(f"  {key}: ✓")

    if caught_up:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            f"Server {failed_server} successfully recovered and caught up",
        )
    else:
        return TestResult(
            test_name, 8, test_max_points, 
            f"Server {failed_server} did not fully catch up"
        )

def test_data_consistency_after_failures():
    """Test 6: Data Consistency Maintained After Failures (15 points)"""
    print("\n=== Test: Data Consistency After Failures ===")

    test_name = "Consistency After Failures"
    test_max_points = 15

    # Start fresh cluster to ensure all servers are alive
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )
    
    time.sleep(config.config["timeouts"]["startup_wait"])

    # Find current leader
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=10)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Write initial data
    test_data = {}
    print("Writing initial data...")
    for i in range(5):
        key = f"consistency_key_{i}"
        value = f"value_{i}"
        test_data[key] = value
        if not server_put(leader_id, key, value):
            return TestResult(
                test_name, 3, test_max_points, "Failed to write initial data"
            )
        time.sleep(0.3)

    time.sleep(2)  # Let it replicate

    # Kill leader and wait for new leader
    print(f"Killing leader (Server {leader_id})...")
    kill_server(leader_id)
    time.sleep(1)

    active_servers = [i for i in range(5) if i != leader_id]
    success, new_leader, _, _ = wait_for_leader_election(
        5,
        timeout_seconds=config.config["timeouts"]["failover_timeout"],
        active_servers=active_servers
    )

    if not success:
        return TestResult(
            test_name, 5, test_max_points, "No new leader elected"
        )

    # Verify all data is consistent across remaining servers
    print("Verifying data consistency across all active servers...")
    all_consistent = True
    
    for key, expected_value in test_data.items():
        values = {}
        for server_id in active_servers:
            success, _, value = server_get(server_id, key)
            if success:
                values[server_id] = value
        
        unique_values = set(values.values())
        if len(unique_values) > 1:
            all_consistent = False
            print(f"  {key}: INCONSISTENT - {values}")
        elif expected_value not in unique_values:
            all_consistent = False
            print(f"  {key}: WRONG VALUE - expected '{expected_value}', got {unique_values}")
        else:
            print(f"  {key}: ✓ consistent")

    if all_consistent:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            "Data remains consistent after leader failure",
        )
    else:
        return TestResult(
            test_name, 10, test_max_points, 
            "Data inconsistency detected after failure"
        )

def test_multiple_failure_scenarios():
    """Test 7: Multiple Sequential Failures (10 points)"""
    print("\n=== Test: Multiple Sequential Failures ===")

    test_name = "Multiple Failures"
    test_max_points = 10

    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Find initial leader
    success, leader1, _, _ = wait_for_leader_election(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No initial leader"
        )

    # First failure: kill leader
    print(f"First failure: killing leader {leader1}")
    kill_server(leader1)
    time.sleep(1)

    # Wait for new leader
    active1 = [i for i in range(5) if i != leader1]
    success, leader2, _, _ = wait_for_leader_election(
        5, 
        timeout_seconds=config.config["timeouts"]["failover_timeout"],
        active_servers=active1
    )
    if not success:
        return TestResult(
            test_name, 3, test_max_points, "No leader after first failure"
        )

    print(f"New leader: {leader2}")

    # Write data
    test_key = "multi_failure_test"
    test_value = "survived_failures"
    if not server_put(leader2, test_key, test_value):
        return TestResult(
            test_name, 5, test_max_points, "Cannot write after first failure"
        )

    time.sleep(2)

    # Second failure: kill another server (not leader)
    non_leaders = [i for i in active1 if i != leader2]
    failed2 = non_leaders[0]
    print(f"Second failure: killing server {failed2}")
    kill_server(failed2)
    time.sleep(1)

    # Verify still operational
    active2 = [i for i in active1 if i != failed2]
    success, _, value = server_get(leader2, test_key)
    
    if not success or value != test_value:
        return TestResult(
            test_name, 7, test_max_points, 
            "Data lost after multiple failures"
        )

    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Cluster survives multiple sequential failures",
    )

def test_split_brain_prevention():
    """Test 8: Split Brain Prevention (10 points)"""
    print("\n=== Test: Split Brain Prevention ===")

    test_name = "Split Brain Prevention"
    test_max_points = 10

    # Start fresh cluster to avoid inheriting dead servers from previous tests
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )
    
    # Give more time for cluster to fully stabilize after previous tests
    time.sleep(config.config["timeouts"]["startup_wait"] + 2)

    # Find current leader
    success, leader_id, term, _ = wait_for_leader_election(5, timeout_seconds=15)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    print(f"Leader: Server {leader_id}, term {term}")

    # Check that only one leader exists
    leaders = []
    for server_id in range(5):
        success, server_term, is_leader, _, _ = get_server_state(server_id)
        if success and is_leader:
            leaders.append((server_id, server_term))

    if len(leaders) > 1:
        return TestResult(
            test_name, 0, test_max_points, 
            f"Multiple leaders detected: {leaders}"
        )

    # Kill leader and immediately check for split brain
    print(f"Killing leader {leader_id}...")
    kill_server(leader_id)
    
    # During election, briefly check if multiple leaders appear
    print("Monitoring for split brain during election...")
    split_brain_detected = False
    
    for _ in range(10):  # Check 10 times over 5 seconds
        time.sleep(0.5)
        leaders_now = []
        for server_id in range(5):
            if server_id == leader_id:
                continue
            success, server_term, is_leader, _, _ = get_server_state(server_id)
            if success and is_leader:
                leaders_now.append((server_id, server_term))
        
        if len(leaders_now) > 1:
            # Check if they're in different terms (which would be a split brain)
            terms = [t for _, t in leaders_now]
            if len(set(terms)) > 1:
                split_brain_detected = True
                print(f"SPLIT BRAIN DETECTED: {leaders_now}")
                break

    if split_brain_detected:
        return TestResult(
            test_name, 0, test_max_points, 
            "Split brain detected - multiple leaders in different terms"
        )

    # Wait for stable leader with longer timeout (some elections take time after many term changes)
    active_servers = [i for i in range(5) if i != leader_id]
    success, new_leader, new_term, _ = wait_for_leader_election(
        5, timeout_seconds=20, active_servers=active_servers
    )

    if not success:
        # Be lenient - if no split brain detected, give partial credit
        return TestResult(
            test_name, 8, test_max_points, "No split brain detected, but slow re-election"
        )

    # Verify new term is higher
    if new_term <= term:
        return TestResult(
            test_name, 7, test_max_points, 
            f"New term not higher: {new_term} vs {term}"
        )

    print(f"New leader: Server {new_leader}, term {new_term} (no split brain)")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "No split brain - clean leader transition",
    )

def test_majority_loss_handling():
    """Test 9: Handling Majority Loss (10 points)
    """
    print("\n=== Test: Majority Loss Handling ===")

    test_name = "Majority Loss"
    test_max_points = 10

    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )
    
    # Give extra time for cluster to stabilize after all previous tests
    time.sleep(config.config["timeouts"]["startup_wait"] + 3)

    # Find current leader with longer timeout
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=20)
    if not success:
        # If we can't even start the test, give 0 but don't penalize the whole suite
        return TestResult(
            test_name, 0, test_max_points, "Could not establish initial cluster (may be system resource issue)"
        )

    # Kill 3 servers INCLUDING the leader to lose majority (leaving only 2/5 alive)
    non_leaders = [i for i in range(5) if i != leader_id]
    servers_to_kill = non_leaders[:2] + [leader_id] # Kill 2 followers, followed by the leader
    
    print(f"Killing majority of servers (including leader): {servers_to_kill}")
    for server_id in servers_to_kill:
        kill_server(server_id)
    
    time.sleep(5)  # Give time for election attempts to fail

    # Remaining servers should not have a leader (no majority for election)
    remaining = [i for i in range(5) if i not in servers_to_kill]
    print(f"Checking remaining servers {remaining} for leader...")
    
    leaders = []
    for server_id in remaining:
        success, term, is_leader, _, _ = get_server_state(server_id)
        if success and is_leader:
            leaders.append(server_id)

    if len(leaders) > 0:
        return TestResult(
            test_name, 5, test_max_points, 
            f"Leader exists without majority: {leaders}"
        )

    print("No leader with minority - correct behavior")

    # Restart servers to restore majority
    print("Restarting servers to restore majority...")
    for server_id in servers_to_kill[:2]:  # Restart 2 to get majority back (2+2=4/5)
        call_start_server(server_id)
        time.sleep(2)  # Give each server more time to start

    # Wait for new leader with longer timeout
    time.sleep(5)
    active_servers = [i for i in range(5) if i not in [servers_to_kill[2]]]
    success, new_leader, _, _ = wait_for_leader_election(
        5, timeout_seconds=20, active_servers=active_servers
    )

    if not success:
        return TestResult(
            test_name, 8, test_max_points, 
            "Correctly blocked without majority, but slow recovery"
        )

    print(f"Leader {new_leader} elected after restoring majority")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Correctly handles majority loss and recovery",
    )

def main():
    print("Assignment 5 Language-Agnostic Test Suite")
    print("Fault Tolerance and Recovery")
    print("=" * 80)
    
    # Initialize configuration
    try:
        init_config()
        print(f"Configuration loaded successfully")
    except Exception as e:
        print(f"FATAL ERROR: Configuration failed: {e}")
        return
    
    # Register cleanup function
    atexit.register(cleanup_all)
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, lambda s, f: (cleanup_all(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (cleanup_all(), sys.exit(0)))
    
    # Clean up any existing processes first
    cleanup_all_processes()
    
    print("\nStarting Assignment 5 tests...\n")
    
    # Initialize test suite
    suite = TestSuite()
    
    try:
        # Run tests
        suite.add(test_basic_cluster_startup())
        suite.add(test_leader_failure_detection())
        suite.add(test_operations_after_leader_failure())
        suite.add(test_minority_failure_tolerance())
        suite.add(test_server_recovery_and_catchup())
        suite.add(test_data_consistency_after_failures())
        suite.add(test_multiple_failure_scenarios())
        suite.add(test_split_brain_prevention())
        suite.add(test_majority_loss_handling())
    
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n\nUnexpected error during testing: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup will be handled by atexit
        pass
    
    # Print results
    suite.print_results()

if __name__ == "__main__":
    main()
