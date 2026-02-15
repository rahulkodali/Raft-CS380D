#!/usr/bin/env python3
"""
Assignment 3 Language-Agnostic Test Script
Tests leader election implementation
"""

import subprocess
import time
import grpc
import sys
import os
import signal
import atexit
import json
import shlex
from datetime import datetime
import configparser
import random
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
                "command": ["python3", "frontend.py"],
                "working_dir": ".",
                "env": {},
                "process_name_pattern": "frontend",
                "startup_timeout": 30
            },
            "server": {
                "command_template": ["python3", "server.py", "{server_id}"],
                "working_dir": ".",
                "env": {},
                "process_name_pattern": "server",
                "startup_timeout": 10
            },
            "ports": {
                "frontend_port": 8001,
                "base_server_port": 9001
            },
            "timeouts": {
                "rpc_timeout": 5,
                "startup_wait": 3,
                "server_ready_timeout": 20,
                "leader_election_timeout": 15,  # Time to wait for leader election
                "leader_stability_timeout": 5   # Time to verify leader stability
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
        print(f"\n===== ASSIGNMENT 3 TEST RESULTS ({now}) =====")
        
        passed = 0
        for r in self.results:
            if r.score == r.max_points:
                status = "PASS"
                passed += 1
            elif r.score > 0:
                status = "PART"
            else:
                status = "FAIL"
            
            print(f"{r.name:<35} [{status}] {r.score:.1f}/{r.max_points:.1f} — {r.details}")
        
        total_tests = len(self.results)
        failed = total_tests - passed
        pass_rate = (passed / total_tests * 100) if total_tests > 0 else 0
        
        print(f"\nTests run: {total_tests}, Passed: {passed}, Failed: {failed} ({pass_rate:.1f}% pass rate)")
        print(f"Overall Score: {self.total:.1f}/100")
        print("=" * 60)

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
        patterns.append(frontend_cmd[-1])  # Last part of command (usually filename)
    
    server_cmd_template = config.config['server']['command_template']
    if len(server_cmd_template) > 0:
        # Remove the {server_id} placeholder for pattern matching
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
    
    time.sleep(2)  # Allow processes to terminate

def start_frontend():
    """Start the frontend service automatically"""
    global frontend_process
    
    print("Starting frontend service...")
    
    try:
        cmd = config.get_frontend_command()
        working_dir = config.get_working_dir('frontend')
        env = config.get_env('frontend')
        
        print(f"Frontend command: {cmd}")
        print(f"Working directory: {working_dir}")
        
        # Start frontend service
        frontend_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            env=env,
            preexec_fn=os.setsid  # Create new process group for easier cleanup
        )
        
        # Wait for frontend to be ready
        frontend_port = config.config['ports']['frontend_port']
        startup_timeout = min(15, config.config['frontend']['startup_timeout'])
        
        print(f"Waiting for frontend on port {frontend_port}...")
        
        for attempt in range(startup_timeout):
            try:
                channel = grpc.insecure_channel(f'localhost:{frontend_port}')
                stub = raft_pb2_grpc.FrontEndStub(channel)
                
                # Try a simple call to verify it's responding
                request = raft_pb2.GetKey(key="test", clientId=1, requestId=1)
                response = stub.Get(request, timeout=10)
                channel.close()
                
                # Any response means it's working
                print(f"Frontend service ready after {attempt + 1} seconds")
                return True
            except Exception as e:
                if attempt == 0:
                    print("Waiting for frontend service to start...", end="")
                elif attempt % 5 == 0:
                    print(f"\n  Still waiting... (attempt {attempt + 1}/{startup_timeout})", end="")
                else:
                    print(".", end="")
                
                time.sleep(1)
        
        print(f"\nERROR: Frontend service failed to start after {startup_timeout} seconds")
        return False
        
    except Exception as e:
        print(f"ERROR: Failed to start frontend service: {e}")
        return False

def call_start_raft(n):
    """Call StartRaft RPC with n servers"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        
        channel = grpc.insecure_channel(f'localhost:{frontend_port}')
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
        
        addr = f"localhost:{base_port + server_id}"
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
        
        addr = f"localhost:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.Empty()
        response = stub.GetState(request, timeout=rpc_timeout)
        channel.close()
        
        return True, response.term, response.isLeader
    except Exception as e:
        return False, 0, False

def call_request_vote(server_id, candidate_term, candidate_id):
    """Call RequestVote RPC on a specific server"""
    try:
        base_port = config.config['ports']['base_server_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        addr = f"localhost:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.RequestVoteArgs(
            term=candidate_term,
            candidateId=candidate_id,
            lastLogIndex=0,  # For Assignment 3, log is empty
            lastLogTerm=0
        )
        response = stub.RequestVote(request, timeout=rpc_timeout)
        channel.close()
        
        return True, response.term, response.voteGranted
    except Exception as e:
        return False, 0, False

def count_responsive_servers(max_servers):
    """Count how many servers are responsive"""
    responsive = []
    for i in range(max_servers):
        if ping_server(i):
            responsive.append(i)
    return responsive

def wait_for_leader_election(num_servers, timeout_seconds=15):
    """Wait for leader election to complete and return leader info"""
    print(f"Waiting up to {timeout_seconds}s for leader election...")
    
    start_time = time.time()
    election_timeout = timeout_seconds
    
    while time.time() - start_time < election_timeout:
        leaders = []
        server_states = {}
        
        # Check all servers for their current state
        for server_id in range(num_servers):
            success, term, is_leader = get_server_state(server_id)
            if success:
                server_states[server_id] = {'term': term, 'is_leader': is_leader}
                if is_leader:
                    leaders.append(server_id)
        
        print(f"  Election check: {len(leaders)} leaders found, states: {server_states}")
        
        # Check if we have exactly one leader
        if len(leaders) == 1:
            leader_id = leaders[0]
            leader_term = server_states[leader_id]['term']
            print(f"Leader elected: Server {leader_id} in term {leader_term}")
            return True, leader_id, leader_term, server_states
        elif len(leaders) > 1:
            print(f"Multiple leaders detected: {leaders} - waiting for resolution...")
        
        time.sleep(1)
    
    print(f"Leader election timed out after {election_timeout}s")
    return False, None, None, server_states

def verify_no_split_brain(num_servers, duration_seconds=3):
    """Verify that there's never more than one leader at any time"""
    print(f"Verifying no split-brain for {duration_seconds}s...")
    
    start_time = time.time()
    checks_passed = 0
    
    while time.time() - start_time < duration_seconds:
        leaders = []
        server_states = {}
        
        # Check all servers
        for server_id in range(num_servers):
            success, term, is_leader = get_server_state(server_id)
            if success:
                server_states[server_id] = {'term': term, 'is_leader': is_leader}
                if is_leader:
                    leaders.append(server_id)
        
        # Verify at most one leader
        if len(leaders) > 1:
            print(f"SPLIT BRAIN DETECTED: Multiple leaders {leaders} at the same time")
            print(f"Server states: {server_states}")
            return False
        
        checks_passed += 1
        time.sleep(0.3)
    
    print(f"No split-brain detected over {checks_passed} checks")
    return True

def test_config_file():
    """Test 1: Configuration File (10 points)"""
    print("\n=== Test: Configuration File ===")

    test_name = "Config File"
    test_max_points = 10
    file_existence_points = 1
    filename_config = "config.ini"

    if os.path.exists(filename_config):
        try:
            cfg = configparser.ConfigParser()
            cfg.read(filename_config)

            required_entries = [
                ("Global", "base_address"),
                ("Servers", "base_port"),
                ("Servers", "base_source_port"),
                ("Servers", "max_workers"),
                ("Servers", "persistent_state_path"),
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
                    "Config file has all required sections and keys.",
                )
            else:
                potential_points = test_max_points - file_existence_points
                potential_points_each = potential_points / len(required_entries)
                points = (
                    file_existence_points
                    + (len(required_entries) - len(missing)) * potential_points_each
                )

                return TestResult(
                    test_name,
                    points,
                    test_max_points,
                    f"Config file missing: {missing}",
                )
        except Exception as e:
            return TestResult(
                test_name,
                file_existence_points,
                test_max_points,
                f"Cannot read config file: {e}",
            )
    else:
        return TestResult(test_name, 0, test_max_points, f"Config file {filename_config} missing.")

def test_server_startup():
    """Test 2: Server Startup and Basic Connectivity (15 points)"""
    print("\n=== Test: Server Startup and Connectivity ===")

    test_name = "Server Startup"
    test_max_points = 15

    # Start frontend first
    if not start_frontend():
        return TestResult(
            test_name, 0, test_max_points, "Frontend service failed to start"
        )

    # Start a 3-server cluster
    print("Starting 3-server cluster...")
    success, error = call_start_raft(3)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"StartRaft(3) failed: {error}"
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Test basic connectivity
    responsive_servers = count_responsive_servers(3)
    print(f"Servers responding to ping: {responsive_servers}")

    if len(responsive_servers) != 3:
        return TestResult(
            test_name, 5, test_max_points, f"Only {len(responsive_servers)}/3 servers responsive"
        )

    # Test GetState RPC
    state_working = 0
    for server_id in range(3):
        success, term, is_leader = get_server_state(server_id)
        if success:
            state_working += 1
            print(f"Server {server_id}: term={term}, leader={is_leader}")

    if state_working == 3:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            "All servers started and respond to ping and GetState.",
        )
    else:
        return TestResult(
            test_name, 10, test_max_points, f"Only {state_working}/3 servers respond to GetState"
        )

def test_leader_election_basic():
    """Test 3: Basic Leader Election (25 points)"""
    print("\n=== Test: Basic Leader Election ===")

    test_name = "Leader Election Basic"
    test_max_points = 25

    # Wait for leader election to complete
    success, leader_id, leader_term, server_states = wait_for_leader_election(3)
    
    if not success:
        return TestResult(
            test_name, 0, test_max_points, 
            f"No leader elected within timeout. Final states: {server_states}"
        )

    print(f"Leader elected: Server {leader_id} in term {leader_term}")

    # Verify exactly one leader
    leaders = [sid for sid, state in server_states.items() if state.get('is_leader', False)]
    if len(leaders) != 1:
        return TestResult(
            test_name, 10, test_max_points, 
            f"Expected exactly 1 leader, found {len(leaders)}: {leaders}"
        )

    # Verify all servers agree on term
    terms = [state['term'] for state in server_states.values()]
    if len(set(terms)) > 1:
        return TestResult(
            test_name, 15, test_max_points, 
            f"Servers have different terms: {terms}"
        )

    # Verify no split-brain scenario occurs
    if not verify_no_split_brain(3):
        return TestResult(
            test_name, 20, test_max_points, 
            f"Split-brain detected during election period"
        )

    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"Leader election successful. Server {leader_id} elected leader in term {leader_term}. No split-brain detected.",
    )

def test_leader_election_larger_cluster():
    """Test 4: Leader Election in Larger Cluster (20 points)"""
    print("\n=== Test: Leader Election in 5-Server Cluster ===")

    test_name = "Leader Election 5-Server"
    test_max_points = 20

    # Start a 5-server cluster
    print("Starting 5-server cluster...")
    success, error = call_start_raft(5)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"StartRaft(5) failed: {error}"
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Check server connectivity
    responsive_servers = count_responsive_servers(5)
    if len(responsive_servers) < 5:
        return TestResult(
            test_name, 5, test_max_points, f"Only {len(responsive_servers)}/5 servers responsive"
        )

    # Wait for leader election
    success, leader_id, leader_term, server_states = wait_for_leader_election(5)
    
    if not success:
        return TestResult(
            test_name, 10, test_max_points, 
            f"No leader elected in 5-server cluster. Final states: {server_states}"
        )

    # Verify exactly one leader
    leaders = [sid for sid, state in server_states.items() if state.get('is_leader', False)]
    if len(leaders) != 1:
        return TestResult(
            test_name, 15, test_max_points, 
            f"Expected exactly 1 leader, found {len(leaders)}: {leaders}"
        )

    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"5-server leader election successful. Server {leader_id} elected leader in term {leader_term}.",
    )

def test_request_vote_rpc():
    """Test 5: RequestVote RPC Functionality (15 points)"""
    print("\n=== Test: RequestVote RPC Functionality ===")

    test_name = "RequestVote RPC"
    test_max_points = 15

    # Test RequestVote RPC directly
    print("Testing RequestVote RPC...")

    # Try to get vote from server 0 for candidate 1 in term 1
    success, response_term, vote_granted = call_request_vote(0, 1, 1)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "RequestVote RPC call failed"
        )

    print(f"RequestVote response: term={response_term}, voteGranted={vote_granted}")

    # The response should be valid (either vote granted or not, but RPC should work)
    if response_term < 1:
        return TestResult(
            test_name, 5, test_max_points, f"Invalid response term: {response_term}"
        )

    # Test vote for same candidate again - should not be granted if already voted
    success2, response_term2, vote_granted2 = call_request_vote(0, 1, 2)  # Different candidate
    if not success2:
        return TestResult(
            test_name, 10, test_max_points, "Second RequestVote RPC call failed"
        )

    print(f"Second RequestVote response: term={response_term2}, voteGranted={vote_granted2}")

    # Test vote with higher term
    success3, response_term3, vote_granted3 = call_request_vote(0, 2, 1)
    if not success3:
        return TestResult(
            test_name, 12, test_max_points, "Higher term RequestVote RPC call failed"
        )

    print(f"Higher term RequestVote response: term={response_term3}, voteGranted={vote_granted3}")

    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "RequestVote RPC working correctly with proper term handling.",
    )

def test_election_timeout():
    """Test 6: Election Timeout and Re-election (10 points)"""
    print("\n=== Test: Election Timeout and Re-election ===")

    test_name = "Election Timeout"
    test_max_points = 10
    
    # Start fresh 3-server cluster
    print("Starting 3-server cluster...")
    success, error = call_start_raft(3)
    if not success:
        return TestResult(test_name, 0, test_max_points, f"StartRaft(3) failed: {error}")
    
    time.sleep(config.config["timeouts"]["startup_wait"])
    
    # Now wait for election with correct cluster size
    success, leader_id, leader_term, server_states = wait_for_leader_election(3)
    
    if not success:
        return TestResult(
            test_name, 0, test_max_points, "No initial leader elected"
        )

    print(f"Initial leader: Server {leader_id} in term {leader_term}")

    # Wait a bit longer to see if election is stable
    print("Waiting to verify election stability...")
    time.sleep(5)

    # Check if leader is still the same
    final_success, final_leader, final_term, final_states = wait_for_leader_election(3, timeout_seconds=5)
    
    if not final_success:
        return TestResult(
            test_name, 5, test_max_points, "Leader election became unstable"
        )

    # Verify term progression makes sense
    if final_term < leader_term:
        return TestResult(
            test_name, 7, test_max_points, f"Term decreased: {leader_term} -> {final_term}"
        )

    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"Election timeout handling works correctly. Final leader: Server {final_leader} in term {final_term}.",
    )

def test_state_transitions():
    """Test 7: Server State Transitions (5 points)"""
    print("\n=== Test: Server State Transitions ===")

    test_name = "State Transitions"
    test_max_points = 5

    # Start fresh 3-server cluster for this test
    print("Starting 3-server cluster for state transitions test...")
    success, error = call_start_raft(3)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Failed to start cluster: {error}"
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Wait for election to complete
    success, leader_id, leader_term, server_states = wait_for_leader_election(3, timeout_seconds=10)
    
    if not success:
        return TestResult(
            test_name, 1, test_max_points, "No leader elected for state transitions test"
        )

    print(f"Leader elected: Server {leader_id} in term {leader_term}")

    # Verify exactly one leader
    leaders = [sid for sid, state in server_states.items() if state.get('is_leader', False)]
    
    if len(leaders) == 1:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            f"Server state transitions working correctly - Server {leader_id} is leader in term {leader_term}.",
        )
    elif len(leaders) == 0:
        return TestResult(
            test_name, 2, test_max_points, "No leader found after election period"
        )
    else:
        return TestResult(
            test_name, 3, test_max_points, f"Multiple leaders found: {leaders}"
        )

def main():
    print("Assignment 3 Language-Agnostic Test Suite")
    print("Leader Election Algorithm")
    print("=" * 60)
    
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
    
    print("\nStarting Assignment 3 tests...\n")
    
    # Initialize test suite
    suite = TestSuite()
    
    try:
        # Run tests
        suite.add(test_config_file())
        suite.add(test_server_startup())
        suite.add(test_leader_election_basic())
        suite.add(test_leader_election_larger_cluster())
        suite.add(test_request_vote_rpc())
        suite.add(test_election_timeout())
        suite.add(test_state_transitions())
    
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