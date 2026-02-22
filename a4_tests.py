#!/usr/bin/env python3
"""
Assignment 4 Language-Agnostic Test Script
Tests log replication and basic operations
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
import shlex
from datetime import datetime
import configparser
import random
import string
import threading
from collections import defaultdict

# Import the generated protobuf files (assuming they exist)
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
                "replication_timeout": 10,  # Time to wait for log replication
                "commit_timeout": 10         # Time to wait for commits
            }
        }
        
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    user_config = json.load(f)
                # Merge user config with defaults
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
        
        # Validate command templates
        if 'command' not in self.config['frontend']:
            raise ValueError("Missing frontend.command in config")
        
        if 'command_template' not in self.config['server']:
            raise ValueError("Missing server.command_template in config")
        
        # Ensure server command template has placeholder
        server_cmd = ' '.join(self.config['server']['command_template'])
        if '{server_id}' not in server_cmd:
            raise ValueError("server.command_template must contain {server_id} placeholder")
    
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
        print(f"\n===== ASSIGNMENT 4 TEST RESULTS ({now}) =====")
        
        passed = 0
        for r in self.results:
            if r.score == r.max_points:
                status = "PASS"
                passed += 1
            elif r.score > 0:
                status = "PART"
            else:
                status = "FAIL"
            
            print(f"{r.name:<40} [{status}] {r.score:.1f}/{r.max_points:.1f} — {r.details}")
        
        total_tests = len(self.results)
        failed = total_tests - passed
        pass_rate = (passed / total_tests * 100) if total_tests > 0 else 0
        
        print(f"\nTests run: {total_tests}, Passed: {passed}, Failed: {failed} ({pass_rate:.1f}% pass rate)")
        print(f"Overall Score: {self.total:.1f}/100")
        print("=" * 70)

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
    
    # Add command-based patterns
    frontend_cmd = config.get_frontend_command()
    if len(frontend_cmd) > 0:
        patterns.append(frontend_cmd[-1])
    
    server_cmd_template = config.config['server']['command_template']
    if len(server_cmd_template) > 0:
        server_pattern = server_cmd_template[-1].replace('{server_id}', '')
        if server_pattern:
            patterns.append(server_pattern)
    
    print(f"Cleaning up all processes matching patterns: {patterns}")
    
    for pattern in patterns:
        if pattern:
            try:
                subprocess.run(['pkill', '-f', pattern], 
                             capture_output=True, timeout=5)
                print(f"  Cleaned up processes matching '{pattern}'")
            except:
                pass
    
    time.sleep(2)

def start_frontend():
    """Start the frontend service if not already running; otherwise reuse it."""
    global frontend_process

    print("Starting frontend service...")
    import sys, socket

    def probe():
        # TCP-level probe first (fast and robust)
        try:
            with socket.create_connection(("127.0.0.1", config.config['ports']['frontend_port']), timeout=0.5):
                return True
        except Exception:
            pass
        # gRPC probe (FrontEnd.Get expects GetKey by your proto)
        try:
            ch = grpc.insecure_channel(f"127.0.0.1:{config.config['ports']['frontend_port']}")
            stub = raft_pb2_grpc.FrontEndStub(ch)
            req = raft_pb2.GetKey(key="__probe__", clientId=0, requestId=0)
            _ = stub.Get(req, timeout=0.8)
            ch.close()
            return True
        except Exception:
            return False

    # If something is already listening, reuse it.
    if probe():
        print("Frontend already running; reusing existing instance.")
        return True

    cmd = config.get_frontend_command()
    working_dir = config.get_working_dir('frontend')
    env = config.get_env('frontend')

    print("Python interpreter:", sys.executable)
    print("Frontend command:", cmd)
    print("Working directory:", working_dir)

    frontend_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=working_dir,
        env=env,
        preexec_fn=os.setsid,
        text=True,  # decode to str
        bufsize=1   # line-buffered
    )

    # Wait for it to come up
    deadline = time.time() + config.config['frontend']['startup_timeout']
    printed_once = False
    while time.time() < deadline:
        if probe():
            print("Frontend service ready.")
            return True

        # If process exited, dump logs and fail
        rc = frontend_process.poll()
        if rc is not None:
            out = frontend_process.stdout.read() or ""
            err = frontend_process.stderr.read() or ""
            print("\n--- frontend stdout ---\n", out)
            print("\n--- frontend stderr ---\n", err)
            print(f"\nERROR: Frontend exited early (rc={rc})")
            return False

        if not printed_once:
            print("Waiting for frontend service to start...", end="", flush=True)
            printed_once = True
        else:
            print(".", end="", flush=True)
        time.sleep(0.5)

    # Timeout: dump whatever logs we have
    out = frontend_process.stdout.read() or ""
    err = frontend_process.stderr.read() or ""
    print("\n--- frontend stdout ---\n", out)
    print("\n--- frontend stderr ---\n", err)
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
        print(f"Server PUT error: {e}")
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

def wait_for_leader_election(num_servers, timeout_seconds=15):
    """Wait for leader election to complete and return leader info"""
    print(f"Waiting up to {timeout_seconds}s for leader election...")
    
    start_time = time.time()
    
    while time.time() - start_time < timeout_seconds:
        leaders = []
        server_states = {}
        
        for server_id in range(num_servers):
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
        
        time.sleep(1)
    
    print(f"Leader election timed out after {timeout_seconds}s")
    return False, None, None, {}

def wait_for_commit(num_servers, expected_commit_index, timeout_seconds=10):
    """Wait for all servers to reach expected commit index"""
    print(f"Waiting for servers to reach commit index {expected_commit_index}...")
    
    start_time = time.time()
    
    while time.time() - start_time < timeout_seconds:
        all_committed = True
        commit_indices = {}
        
        for server_id in range(num_servers):
            success, term, is_leader, commit_idx, last_applied = get_server_state(server_id)
            if success:
                commit_indices[server_id] = commit_idx
                if commit_idx < expected_commit_index:
                    all_committed = False
            else:
                all_committed = False
        
        if all_committed:
            print(f"All servers reached commit index {expected_commit_index}")
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

# Test functions for Assignment 4

def test_config_file():
    """Test 1: Configuration File (5 points)"""
    print("\n=== Test: Configuration File ===")

    test_name = "Config File"
    test_max_points = 5

    if os.path.exists("config.ini"):
        try:
            cfg = configparser.ConfigParser()
            cfg.read("config.ini")

            required_entries = [
                ("Global", "base_address"),
                ("Servers", "base_port"),
                ("Servers", "active"),
            ]

            missing = []
            for section, key in required_entries:
                if not cfg.has_option(section, key):
                    missing.append(f"{section}.{key}")

            if not missing:
                return TestResult(
                    test_name,
                    test_max_points,
                    test_max_points,
                    "Config file valid.",
                )
            else:
                return TestResult(
                    test_name,
                    2,
                    test_max_points,
                    f"Config file missing: {missing}",
                )
        except Exception as e:
            return TestResult(
                test_name,
                1,
                test_max_points,
                f"Cannot read config: {e}",
            )
    else:
        return TestResult(test_name, 0, test_max_points, "Config file missing.")

def test_server_startup_and_election():
    """Test 2: Server Startup and Leader Election (10 points)"""
    print("\n=== Test: Server Startup and Leader Election ===")

    test_name = "Startup & Election"
    test_max_points = 10

    # Start frontend
    if not start_frontend():
        return TestResult(
            test_name, 0, test_max_points, "Frontend failed to start"
        )

    # Start 3-server cluster
    print("Starting 3-server cluster...")
    success, error = call_start_raft(3)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"StartRaft(3) failed: {error}"
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Wait for leader election
    success, leader_id, leader_term, server_states = wait_for_leader_election(3)
    
    if not success:
        return TestResult(
            test_name, 5, test_max_points, "No leader elected"
        )

    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"Cluster started, leader elected: Server {leader_id}.",
    )

def test_basic_put_operation():
    """Test 3: Basic Put Operation (15 points)"""
    print("\n=== Test: Basic Put Operation ===")

    test_name = "Basic Put"
    test_max_points = 15

    # Find leader
    success, leader_id, leader_term, server_states = wait_for_leader_election(3, timeout_seconds=5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Perform Put operation on leader
    test_key = "test_put_key"
    test_value = "test_put_value"
    
    print(f"Sending PUT to leader (server {leader_id}): {test_key} -> {test_value}")
    if not server_put(leader_id, test_key, test_value):
        return TestResult(
            test_name, 5, test_max_points, "PUT operation failed on leader"
        )

    # Wait for replication/commit
    time.sleep(2)

    # Verify value can be retrieved from leader
    success, ret_key, ret_value = server_get(leader_id, test_key)
    if not success:
        return TestResult(
            test_name, 10, test_max_points, "GET after PUT failed"
        )

    if ret_value != test_value:
        return TestResult(
            test_name, 10, test_max_points, 
            f"GET returned wrong value: expected '{test_value}', got '{ret_value}'"
        )

    print(f"PUT and GET successful on leader")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Basic PUT operation successful.",
    )

def test_log_replication():
    """Test 4: Log Replication to Followers (20 points)"""
    print("\n=== Test: Log Replication ===")

    test_name = "Log Replication"
    test_max_points = 20

    # Find leader
    success, leader_id, leader_term, server_states = wait_for_leader_election(3, timeout_seconds=5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Put a value on leader
    test_key = "replication_key"
    test_value = "replication_value"
    
    print(f"Sending PUT to leader: {test_key} -> {test_value}")
    if not server_put(leader_id, test_key, test_value):
        return TestResult(
            test_name, 5, test_max_points, "PUT operation failed"
        )

    # Wait for replication
    print("Waiting for log replication...")
    time.sleep(3)

    # Check that value is available on all servers
    consistent_count = 0
    for server_id in range(3):
        success, ret_key, ret_value = server_get(server_id, test_key)
        if success and ret_value == test_value:
            consistent_count += 1
            print(f"  Server {server_id}: ✓ value replicated")
        else:
            print(f"  Server {server_id}: ✗ value not replicated (got '{ret_value}')")

    if consistent_count == 3:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            "Value successfully replicated to all servers.",
        )
    elif consistent_count >= 2:
        return TestResult(
            test_name, 15, test_max_points, 
            f"Value replicated to {consistent_count}/3 servers (majority achieved)"
        )
    else:
        return TestResult(
            test_name, 5, test_max_points, 
            f"Value only on {consistent_count}/3 servers (no majority)"
        )

def test_get_consistency():
    """Test 5: Get Consistency Across Servers (15 points)"""
    print("\n=== Test: Get Consistency ===")

    test_name = "Get Consistency"
    test_max_points = 15

    # Find leader
    success, leader_id, leader_term, server_states = wait_for_leader_election(3, timeout_seconds=5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Put multiple values
    test_data = {
        "consistency_key1": "value1",
        "consistency_key2": "value2",
        "consistency_key3": "value3"
    }

    print("Writing test data to leader...")
    for key, value in test_data.items():
        if not server_put(leader_id, key, value):
            return TestResult(
                test_name, 5, test_max_points, f"Failed to PUT {key}"
            )
        time.sleep(0.5)  # Small delay between operations

    # Wait for replication
    time.sleep(3)

    # Verify consistency across all servers
    print("Verifying consistency across all servers...")
    all_consistent = True
    
    for key, expected_value in test_data.items():
        values_by_server = {}
        for server_id in range(3):
            success, ret_key, ret_value = server_get(server_id, key)
            if success:
                values_by_server[server_id] = ret_value
        
        unique_values = set(values_by_server.values())
        if len(unique_values) > 1:
            all_consistent = False
            print(f"  {key}: INCONSISTENT - {values_by_server}")
        elif expected_value not in unique_values:
            all_consistent = False
            print(f"  {key}: WRONG VALUE - expected '{expected_value}', got {unique_values}")
        else:
            print(f"  {key}: ✓ consistent across all servers")

    if all_consistent:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            "All servers return consistent values.",
        )
    else:
        return TestResult(
            test_name, 7, test_max_points, 
            "Servers have inconsistent values"
        )

def test_commit_index_progression():
    """Test 6: Commit Index Progression (15 points)"""
    print("\n=== Test: Commit Index Progression ===")

    test_name = "Commit Index"
    test_max_points = 15

    # Find leader
    success, leader_id, leader_term, server_states = wait_for_leader_election(3, timeout_seconds=5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Get initial commit indices
    initial_commits = {}
    for server_id in range(3):
        success, term, is_leader, commit_idx, last_applied = get_server_state(server_id)
        if success:
            initial_commits[server_id] = commit_idx

    print(f"Initial commit indices: {initial_commits}")

    # Perform several Put operations
    num_operations = 3
    print(f"Performing {num_operations} PUT operations...")
    for i in range(num_operations):
        key = f"commit_test_{i}"
        value = f"value_{i}"
        if not server_put(leader_id, key, value):
            return TestResult(
                test_name, 5, test_max_points, f"PUT operation {i} failed"
            )
        time.sleep(0.5)

    # Wait for commits to propagate
    time.sleep(3)

    # Check that commit indices increased
    final_commits = {}
    for server_id in range(3):
        success, term, is_leader, commit_idx, last_applied = get_server_state(server_id)
        if success:
            final_commits[server_id] = commit_idx

    print(f"Final commit indices: {final_commits}")

    # Verify progression
    servers_progressed = 0
    for server_id in range(3):
        if final_commits.get(server_id, 0) > initial_commits.get(server_id, 0):
            servers_progressed += 1
            print(f"  Server {server_id}: commit index increased from {initial_commits.get(server_id, 0)} to {final_commits.get(server_id, 0)}")

    if servers_progressed >= 2:  # At least majority
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            f"Commit indices progressed on {servers_progressed}/3 servers.",
        )
    elif servers_progressed > 0:
        return TestResult(
            test_name, 7, test_max_points, 
            f"Only {servers_progressed}/3 servers progressed commit index"
        )
    else:
        return TestResult(
            test_name, 0, test_max_points, 
            "No servers progressed commit index"
        )

def test_multiple_operations():
    """Test 7: Multiple Sequential Operations (10 points)"""
    print("\n=== Test: Multiple Sequential Operations ===")

    test_name = "Multiple Operations"
    test_max_points = 10

    # Find leader
    success, leader_id, leader_term, server_states = wait_for_leader_election(3, timeout_seconds=5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Generate test data
    keys, values = generate_test_data(5)
    
    print(f"Performing {len(keys)} PUT operations...")
    for i, (key, value) in enumerate(zip(keys, values)):
        if not server_put(leader_id, key, value):
            return TestResult(
                test_name, i * 2, test_max_points, f"PUT {i} failed"
            )
        time.sleep(0.3)

    # Wait for replication
    time.sleep(3)

    # Verify all values
    print("Verifying all values...")
    successful_gets = 0
    for key, expected_value in zip(keys, values):
        success, ret_key, ret_value = server_get(leader_id, key)
        if success and ret_value == expected_value:
            successful_gets += 1
        else:
            print(f"  {key}: expected '{expected_value}', got '{ret_value}'")

    if successful_gets == len(keys):
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            f"All {len(keys)} operations successful.",
        )
    else:
        points = int((successful_gets / len(keys)) * test_max_points)
        return TestResult(
            test_name, points, test_max_points, 
            f"Only {successful_gets}/{len(keys)} operations successful"
        )

def test_state_machine_consistency():
    """Test 8: State Machine Consistency (10 points)"""
    print("\n=== Test: State Machine Consistency ===")

    test_name = "State Machine"
    test_max_points = 10

    # Find leader
    success, leader_id, leader_term, server_states = wait_for_leader_election(3, timeout_seconds=5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No leader available"
        )

    # Write several values
    test_operations = [
        ("sm_key1", "initial_value"),
        ("sm_key1", "updated_value"),  # Overwrite
        ("sm_key2", "value2"),
    ]

    print("Performing state machine operations...")
    for key, value in test_operations:
        if not server_put(leader_id, key, value):
            return TestResult(
                test_name, 3, test_max_points, "PUT operation failed"
            )
        time.sleep(0.5)

    # Wait for commits
    time.sleep(3)

    # Verify final state on all servers
    expected_state = {
        "sm_key1": "updated_value",  # Should be overwritten value
        "sm_key2": "value2"
    }

    print("Verifying state machine consistency...")
    all_consistent = True
    
    for server_id in range(3):
        server_consistent = True
        for key, expected_value in expected_state.items():
            success, ret_key, ret_value = server_get(server_id, key)
            if not success or ret_value != expected_value:
                server_consistent = False
                all_consistent = False
                print(f"  Server {server_id}: {key} = '{ret_value}' (expected '{expected_value}')")
        
        if server_consistent:
            print(f"  Server {server_id}: ✓ state machine consistent")

    if all_consistent:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            "State machine consistent across all servers.",
        )
    else:
        return TestResult(
            test_name, 5, test_max_points, 
            "State machines are inconsistent"
        )

def main():
    print("Assignment 4 Language-Agnostic Test Suite")
    print("Log Replication and Basic Operations")
    print("=" * 70)
    
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
    
    print("\nStarting Assignment 4 tests...\n")
    
    # Initialize test suite
    suite = TestSuite()
    
    try:
        # Run tests
        suite.add(test_config_file())
        suite.add(test_server_startup_and_election())
        suite.add(test_basic_put_operation())
        suite.add(test_log_replication())
        suite.add(test_get_consistency())
        suite.add(test_commit_index_progression())
        suite.add(test_multiple_operations())
        suite.add(test_state_machine_consistency())
    
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