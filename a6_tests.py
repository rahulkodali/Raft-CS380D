#!/usr/bin/env python3
"""
Assignment 6 Language-Agnostic Test Script
Tests persistence, network partitions, and advanced fault tolerance
"""

import subprocess
import time
import grpc
import sys
import os
import shutil
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
import signal
import atexit
import json
import random
import string
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
                "leader_election_timeout": 25,
                "replication_timeout": 10,
                "commit_timeout": 10,
                "recovery_timeout": 25,
                "failover_timeout": 30,
                "persistence_recovery_timeout": 30
            },
            "persistence": {
                "state_dir": "raft_state"
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
        print(f"\n===== ASSIGNMENT 6 TEST RESULTS ({now}) =====")
        
        passed = 0
        for r in self.results:
            if r.score == r.max_points:
                status = "PASS"
                passed += 1
            elif r.score > 0:
                status = "PART"
            else:
                status = "FAIL"
            
            print(f"{r.name:<50} [{status}] {r.score:.1f}/{r.max_points:.1f} — {r.details}")
        
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

def cleanup_persistent_state():
    """Clean up persistent state directory"""
    if not config:
        return
    
    state_dir = config.config.get('persistence', {}).get('state_dir', 'raft_state')
    if os.path.exists(state_dir):
        try:
            shutil.rmtree(state_dir)
            print(f"Cleaned up persistent state directory: {state_dir}")
        except Exception as e:
            print(f"Warning: Could not clean up state directory: {e}")

def verify_persistent_state_exists():
    """Check if persistent state files exist"""
    state_dir = config.config.get('persistence', {}).get('state_dir', 'raft_state')
    if not os.path.exists(state_dir):
        return False, []
    
    state_files = []
    for i in range(5):
        state_file = os.path.join(state_dir, f"server_{i}.json")
        if os.path.exists(state_file):
            state_files.append(i)
    
    return len(state_files) > 0, state_files

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
    """Kill a specific server process"""
    try:
        base_port = config.config['ports']['base_server_port']
        port = base_port + server_id
        
        result = subprocess.run(
            ['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'],
            capture_output=True,
            text=True,
            timeout=2
        )
        
        if result.stdout.strip():
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

def wait_for_leader_election(num_servers, timeout_seconds=15, active_servers=None):
    """Wait for leader election to complete and return leader info"""
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

# ============================================================================
# ASSIGNMENT 6 SPECIFIC TESTS
# ============================================================================

def test_basic_persistence():
    """Test 1: Basic Persistence - Single Server Restart (10 points)"""
    print("\n=== Test: Basic Persistence ===")
    
    test_name = "Basic Persistence"
    test_max_points = 10
    
    # Start frontend
    if not start_frontend():
        return TestResult(test_name, 0, test_max_points, "Frontend failed to start")
    
    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(test_name, 0, test_max_points, f"Failed to start cluster: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    # Wait for leader
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=15)
    if not success:
        return TestResult(test_name, 0, test_max_points, "No leader elected")
    
    # Write data
    test_data = {}
    print("Writing test data...")
    for i in range(3):
        key = f"persist_key_{i}"
        value = f"persist_value_{i}"
        test_data[key] = value
        if not server_put(leader_id, key, value):
            return TestResult(test_name, 3, test_max_points, f"Failed to write key {i}")
        time.sleep(0.5)
    
    time.sleep(2)  # Let it replicate and commit
    
    # Verify persistent state files were created
    exists, state_files = verify_persistent_state_exists()
    if not exists:
        return TestResult(test_name, 5, test_max_points, "No persistent state files created")
    
    print(f"Persistent state files found for servers: {state_files}")
    
    # Kill and restart a non-leader server
    non_leaders = [i for i in range(5) if i != leader_id]
    test_server = non_leaders[0]
    
    print(f"Killing server {test_server}...")
    kill_server(test_server)
    time.sleep(2)
    
    print(f"Restarting server {test_server} (should load from disk)...")
    success, error = call_start_server(test_server)
    if not success:
        return TestResult(test_name, 7, test_max_points, f"Failed to restart server: {error}")
    
    time.sleep(config.config["timeouts"]["recovery_timeout"])
    
    # Verify recovered server has the data
    print(f"Verifying data on recovered server {test_server}...")
    all_correct = True
    for key, expected_value in test_data.items():
        success, _, value = server_get(test_server, key)
        if not success or value != expected_value:
            all_correct = False
            print(f"  {key}: expected '{expected_value}', got '{value}'")
        else:
            print(f"  {key}: ✓")
    
    if all_correct:
        return TestResult(test_name, test_max_points, test_max_points,
                         f"Server {test_server} recovered with persistent state")
    else:
        return TestResult(test_name, 8, test_max_points, 
                         f"Server {test_server} recovered but data incomplete")

def test_full_cluster_restart():
    """Test 2: Full Cluster Restart (20 points)"""
    print("\n=== Test: Full Cluster Restart ===")
    
    test_name = "Full Cluster Restart"
    test_max_points = 20
    
    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(test_name, 0, test_max_points, f"Failed to start cluster: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    # Wait for leader
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=15)
    if not success:
        return TestResult(test_name, 0, test_max_points, "No leader elected")
    
    # Write significant amount of data
    test_data = {}
    print("Writing test data before cluster crash...")
    for i in range(10):
        key = f"crash_test_key_{i}"
        value = f"crash_test_value_{i}"
        test_data[key] = value
        if not server_put(leader_id, key, value):
            return TestResult(test_name, 5, test_max_points, f"Failed to write key {i}")
        time.sleep(0.3)
    
    time.sleep(3)  # Ensure everything is committed and applied
    
    print("Verifying data before crash...")
    for key, expected_value in test_data.items():
        success, _, value = server_get(leader_id, key)
        if not success or value != expected_value:
            return TestResult(test_name, 7, test_max_points, 
                            f"Data not committed before crash: {key}")
    
    # Kill ALL servers
    print("Killing ALL servers...")
    for i in range(5):
        kill_server(i)
    
    time.sleep(3)
    
    # Verify persistent state exists
    exists, state_files = verify_persistent_state_exists()
    if not exists:
        return TestResult(test_name, 10, test_max_points, "No persistent state after cluster crash")
    
    print(f"Persistent state preserved for servers: {state_files}")
    
    # Restart ALL servers
    print("Restarting ALL servers...")
    for i in range(5):
        success, error = call_start_server(i)
        if not success:
            return TestResult(test_name, 12, test_max_points, 
                            f"Failed to restart server {i}: {error}")
        time.sleep(1)
    
    # Wait longer for cluster to stabilize after full restart
    time.sleep(config.config["timeouts"]["persistence_recovery_timeout"])
    
    # Wait for new leader
    success, new_leader, _, _ = wait_for_leader_election(5, timeout_seconds=20)
    if not success:
        return TestResult(test_name, 15, test_max_points, 
                         "No leader elected after cluster restart")
    
    print(f"New leader after restart: Server {new_leader}")
    
    # Verify ALL data survived the crash
    print("Verifying all data survived cluster crash...")
    all_correct = True
    for key, expected_value in test_data.items():
        success, _, value = server_get(new_leader, key)
        if not success or value != expected_value:
            all_correct = False
            print(f"  {key}: LOST or CORRUPTED - expected '{expected_value}', got '{value}'")
        else:
            print(f"  {key}: ✓ survived")
    
    if all_correct:
        return TestResult(test_name, test_max_points, test_max_points,
                         "All data survived full cluster restart")
    else:
        return TestResult(test_name, 17, test_max_points, 
                         "Some data lost after cluster restart")

def test_network_partition_majority():
    """Test 3: Network Partition - Majority Continues (15 points)"""
    print("\n=== Test: Network Partition - Majority ===")
    
    test_name = "Partition: Majority"
    test_max_points = 15
    
    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(test_name, 0, test_max_points, f"Failed to start cluster: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    # Wait for leader
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=15)
    if not success:
        return TestResult(test_name, 0, test_max_points, "No leader elected")
    
    # Write data before partition
    test_key_before = "before_partition"
    test_value_before = "data_before_split"
    print(f"Writing data before partition: {test_key_before}")
    if not server_put(leader_id, test_key_before, test_value_before):
        return TestResult(test_name, 3, test_max_points, "Failed to write data before partition")
    
    time.sleep(2)
    
    # Create 3-2 partition by killing 2 servers
    servers_to_kill = [0, 1]
    
    print(f"Creating network partition: killing servers {servers_to_kill}")
    for server_id in servers_to_kill:
        kill_server(server_id)
    
    time.sleep(2)
    
    # Majority partition: servers 2, 3, 4
    majority = [i for i in range(5) if i not in servers_to_kill]
    
    # Wait for majority to elect leader
    success, maj_leader, _, _ = wait_for_leader_election(
        5, 
        timeout_seconds=config.config["timeouts"]["failover_timeout"],
        active_servers=majority
    )
    
    if not success:
        return TestResult(test_name, 7, test_max_points, 
                         "Majority partition failed to elect leader")
    
    print(f"Majority partition has leader: Server {maj_leader}")
    
    # Try to write data in majority partition
    test_key_during = "during_partition"
    test_value_during = "majority_still_works"
    print(f"Writing data to majority partition: {test_key_during}")
    
    if not server_put(maj_leader, test_key_during, test_value_during):
        return TestResult(test_name, 10, test_max_points, 
                         "Majority partition cannot accept writes")
    
    time.sleep(2)
    
    # Verify data can be read from majority partition
    success1, _, val1 = server_get(maj_leader, test_key_before)
    success2, _, val2 = server_get(maj_leader, test_key_during)
    
    if not success1 or val1 != test_value_before:
        return TestResult(test_name, 12, test_max_points, 
                         "Old data lost in majority partition")
    
    if not success2 or val2 != test_value_during:
        return TestResult(test_name, 12, test_max_points, 
                         "New data not written in majority partition")
    
    print("Majority partition continues operating correctly")
    return TestResult(test_name, test_max_points, test_max_points,
                     "Majority partition operates correctly")

def test_network_partition_minority():
    """Test 4: Network Partition - Minority Cannot Commit (15 points)"""
    print("\n=== Test: Network Partition - Minority ===")

    test_name = "Partition: Minority"
    test_max_points = 15

    try:
        # Start fresh cluster
        print("Starting fresh 5-server cluster...")
        success, error = call_start_raft(5)
        if not success:
            return TestResult(
                test_name, 0, test_max_points, f"StartRaft(5) failed: {error}"
            )

        time.sleep(config.config["timeouts"]["startup_wait"])

        # Wait for leader election
        success, leader_id, leader_term, server_states = wait_for_leader_election(5, timeout_seconds=15)
        if not success:
            return TestResult(
                test_name, 0, test_max_points, "No leader elected"
            )

        # Create partition: kill 3 servers (majority), leaving 2 alive (minority)
        majority_servers = [0, 1, 2]
        minority_servers = [3, 4]
        
        print(f"Creating network partition: killing majority servers {majority_servers}")
        for server_id in majority_servers:
            kill_server(server_id)
        
        time.sleep(3)

        # Test: Can minority commit writes?
        test_key = "minority_write_test"
        test_value = "should_not_commit"
        
        print(f"Attempting write to minority partition {minority_servers}...")
        
        # Try write on any responsive minority server
        write_attempted = False
        write_accepted = False
        target_server = None
        
        for server_id in minority_servers:
            success, term, is_leader, _, _ = get_server_state(server_id)
            if success:
                target_server = server_id
                write_accepted = server_put(server_id, test_key, test_value)
                write_attempted = True
                if write_accepted:
                    print(f"  Server {server_id} accepted write")
                else:
                    print(f"  Server {server_id} rejected write")
                break
        
        if not write_attempted:
            return TestResult(
                test_name, 0, test_max_points,
                "No minority servers responding"
            )
        
        # Wait for potential commit
        time.sleep(3)
        
        # CRITICAL CHECK: Was the write actually committed?
        # Check by reading the key back
        for server_id in minority_servers:
            success, _, value = server_get(server_id, test_key)
            if success and value == test_value:
                # SAFETY VIOLATION: Minority committed the write!
                return TestResult(
                    test_name, 0, test_max_points,
                    f"SAFETY VIOLATION: Minority partition committed write! "
                    f"Server {server_id} returned value '{value}' for key '{test_key}'. "
                    f"Raft requires majority to commit."
                )
        
        # Success: Write was either rejected OR accepted but not committed
        if write_accepted:
            print("Minority accepted write but correctly did not commit (correct)")
        else:
            print("Minority rejected write (correct)")
        
        return TestResult(
            test_name, test_max_points, test_max_points,
            f"Minority partition (servers {minority_servers}) correctly cannot commit writes."
        )

    except Exception as e:
        return TestResult(
            test_name, 0, test_max_points, f"Test failed with exception: {str(e)}"
        )

def test_partition_healing():
    """Test 5: Partition Healing - Minority Accepts Majority Log (20 points)"""
    print("\n=== Test: Partition Healing ===")
    
    test_name = "Partition Healing"
    test_max_points = 20
    
    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(test_name, 0, test_max_points, f"Failed to start cluster: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    # Wait for leader
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=15)
    if not success:
        return TestResult(test_name, 0, test_max_points, "No leader elected")
    
    # Write data before partition
    pre_partition_data = {}
    print("Writing data before partition...")
    for i in range(3):
        key = f"pre_partition_{i}"
        value = f"value_{i}"
        pre_partition_data[key] = value
        if not server_put(leader_id, key, value):
            return TestResult(test_name, 3, test_max_points, f"Failed to write pre-partition data {i}")
        time.sleep(0.3)
    
    time.sleep(2)
    
    # Create partition: kill minority [0, 1]
    minority = [0, 1]
    majority = [2, 3, 4]
    
    print(f"Creating partition: killing minority {minority}")
    for server_id in minority:
        kill_server(server_id)
    
    time.sleep(2)
    
    # Wait for majority to have leader
    success, maj_leader, _, _ = wait_for_leader_election(
        5, timeout_seconds=config.config["timeouts"]["failover_timeout"],
        active_servers=majority
    )
    
    if not success:
        return TestResult(test_name, 5, test_max_points, "Majority failed to elect leader")
    
    # Write data in majority partition
    during_partition_data = {}
    print("Writing data in majority partition...")
    for i in range(3):
        key = f"during_partition_{i}"
        value = f"majority_value_{i}"
        during_partition_data[key] = value
        if not server_put(maj_leader, key, value):
            return TestResult(test_name, 8, test_max_points, 
                            f"Failed to write during-partition data {i}")
        time.sleep(0.3)
    
    time.sleep(2)
    
    # Heal partition: restart minority servers
    print(f"Healing partition: restarting minority servers {minority}")
    for server_id in minority:
        success, error = call_start_server(server_id)
        if not success:
            return TestResult(test_name, 10, test_max_points, 
                            f"Failed to restart minority server {server_id}")
        time.sleep(1)
    
    # Wait for cluster to stabilize
    time.sleep(config.config["timeouts"]["recovery_timeout"])
    
    # Verify all servers have consistent state
    print("Verifying consistency across all servers after healing...")
    all_data = {**pre_partition_data, **during_partition_data}
    
    consistent = True
    for server_id in range(5):
        print(f"Checking server {server_id}...")
        for key, expected_value in all_data.items():
            success, _, value = server_get(server_id, key)
            if not success or value != expected_value:
                consistent = False
                print(f"  Server {server_id} - {key}: expected '{expected_value}', got '{value}'")
            else:
                print(f"  Server {server_id} - {key}: ✓")
    
    if consistent:
        return TestResult(test_name, test_max_points, test_max_points,
                         "Partition healed, all servers consistent")
    else:
        return TestResult(test_name, 15, test_max_points, 
                         "Partition healed but inconsistency remains")

def test_persistence_with_leader_changes():
    """Test 6: Persistence Across Multiple Leader Changes (10 points)"""
    print("\n=== Test: Persistence with Leader Changes ===")
    
    test_name = "Persistence + Leader Changes"
    test_max_points = 10
    
    # Start fresh cluster
    print("Starting fresh 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(test_name, 0, test_max_points, f"Failed to start cluster: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    # Track data written across multiple leaders
    all_data = {}
    
    for round in range(3):
        # Find current leader
        success, leader_id, term, _ = wait_for_leader_election(5, timeout_seconds=15)
        if not success:
            return TestResult(test_name, round * 3, test_max_points, 
                            f"No leader in round {round}")
        
        print(f"Round {round}: Leader is Server {leader_id} (term {term})")
        
        # Write data with this leader
        key = f"leader_change_key_{round}"
        value = f"leader_{leader_id}_term_{term}"
        all_data[key] = value
        
        if not server_put(leader_id, key, value):
            return TestResult(test_name, round * 3 + 1, test_max_points, 
                            f"Failed to write in round {round}")
        
        time.sleep(2)
        
        # Kill the leader to force election
        if round < 2:
            print(f"Killing leader {leader_id}...")
            kill_server(leader_id)
            time.sleep(2)
    
    # Verify all data is present
    available_servers = [i for i in range(5) if ping_server(i)]
    if not available_servers:
        return TestResult(test_name, 7, test_max_points, "No servers available")
    
    test_server = available_servers[0]
    print(f"Verifying all data on server {test_server}...")
    
    all_correct = True
    for key, expected_value in all_data.items():
        success, _, value = server_get(test_server, key)
        if not success or value != expected_value:
            all_correct = False
            print(f"  {key}: expected '{expected_value}', got '{value}'")
        else:
            print(f"  {key}: ✓")
    
    if all_correct:
        return TestResult(test_name, test_max_points, test_max_points,
                         "Data persisted across multiple leader changes")
    else:
        return TestResult(test_name, 8, test_max_points, 
                         "Some data lost during leader changes")

def test_start_raft_clears_state():
    """Test 7: StartRaft Clears Persistent State (10 points)"""
    print("\n=== Test: StartRaft Clears State ===")
    
    test_name = "StartRaft Clears State"
    test_max_points = 10
    
    # Start first cluster and write data
    print("Starting first cluster and writing data...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(test_name, 0, test_max_points, f"Failed to start first cluster: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    success, leader_id, _, _ = wait_for_leader_election(5, timeout_seconds=15)
    if not success:
        return TestResult(test_name, 0, test_max_points, "No leader in first cluster")
    
    # Write data that should be erased
    old_key = "should_be_deleted"
    old_value = "old_data"
    if not server_put(leader_id, old_key, old_value):
        return TestResult(test_name, 2, test_max_points, "Failed to write test data")
    
    time.sleep(2)
    
    # Verify data exists
    success, _, value = server_get(leader_id, old_key)
    if not success or value != old_value:
        return TestResult(test_name, 4, test_max_points, "Test data not written correctly")
    
    print(f"Data written: {old_key} = {old_value}")
    
    # Kill all servers
    print("Killing all servers...")
    for i in range(5):
        kill_server(i)
    
    time.sleep(2)
    
    # Verify state files exist
    exists, _ = verify_persistent_state_exists()
    if not exists:
        return TestResult(test_name, 5, test_max_points, "State files not created")
    
    # Start NEW cluster with StartRaft (should delete state)
    print("Starting NEW cluster with StartRaft (should clear state)...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(test_name, 6, test_max_points, f"Failed to start new cluster: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    success, new_leader, _, _ = wait_for_leader_election(5, timeout_seconds=15)
    if not success:
        return TestResult(test_name, 7, test_max_points, "No leader in new cluster")
    
    # Verify old data is GONE
    success, _, value = server_get(new_leader, old_key)
    if success and value == old_value:
        return TestResult(test_name, 0, test_max_points, 
                         "StartRaft did NOT clear state - old data still exists!")
    
    if not success or value == "":
        print("Old data correctly erased by StartRaft")
        return TestResult(test_name, test_max_points, test_max_points,
                         "StartRaft correctly clears persistent state")
    
    return TestResult(test_name, 8, test_max_points, 
                     "Unclear if state was cleared")

def main():
    print("Assignment 6 Language-Agnostic Test Suite")
    print("Persistence and Advanced Fault Tolerance")
    print("=" * 80)
    
    # Initialize configurations
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
    
    # Clean up any existing processes and state
    cleanup_all_processes()
    cleanup_persistent_state()
    
    print("\nStarting Assignment 6 tests...\n")
    
    # Initialize test suite
    suite = TestSuite()
    
    try:
        # Run tests
        suite.add(test_basic_persistence())
        suite.add(test_full_cluster_restart())
        suite.add(test_network_partition_majority())
        suite.add(test_network_partition_minority())
        suite.add(test_partition_healing())
        suite.add(test_persistence_with_leader_changes())
        suite.add(test_start_raft_clears_state())
    
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n\nUnexpected error during testing: {e}")
        import traceback
        traceback.print_exc()
    finally:
        pass
    
    # Print results
    suite.print_results()

if __name__ == "__main__":
    main()