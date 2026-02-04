#!/usr/bin/env python3
"""
Assignment 2 Language-Agnostic Test Script
Tests single-server key-value store implementation
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
import string

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
                "server_ready_timeout": 20
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
        print(f"\n===== ASSIGNMENT 2 TEST RESULTS ({now}) =====")
        
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
        startup_timeout = config.config['frontend']['startup_timeout']
        
        print(f"Waiting for frontend on port {frontend_port}...")
        
        for attempt in range(startup_timeout):
            try:
                channel = grpc.insecure_channel(f'localhost:{frontend_port}')
                stub = raft_pb2_grpc.FrontEndStub(channel)
                
                # Try a simple call to verify it's responding
                request = raft_pb2.GetKey(key="test", clientId=1, requestId=1)
                response = stub.Get(request, timeout=2)
                channel.close()
                
                # Any response means it's working (even errors are ok)
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

def server_get(server_id, key):
    """Call Get directly on server"""
    try:
        base_port = config.config['ports']['base_server_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        addr = f"localhost:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.StringArg(arg=key)
        response = stub.Get(request, timeout=rpc_timeout)
        channel.close()
        
        return True, response.key, response.value
    except Exception as e:
        return False, "", str(e)

def server_put(server_id, key, value):
    """Call Put directly on server"""
    try:
        base_port = config.config['ports']['base_server_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        addr = f"localhost:{base_port + server_id}"
        channel = grpc.insecure_channel(addr)
        stub = raft_pb2_grpc.KeyValueStoreStub(channel)
        
        request = raft_pb2.KeyValue(key=key, value=value)
        response = stub.Put(request, timeout=rpc_timeout)
        channel.close()
        
        return response.success
    except Exception as e:
        return False

def frontend_get(key):
    """Call Get on frontend"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        channel = grpc.insecure_channel(f'localhost:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.GetKey(key=key, clientId=1, requestId=1)
        response = stub.Get(request, timeout=rpc_timeout)
        channel.close()
        
        if response.wrongLeader:
            return False, "", response.error
        return True, response.value, ""
    except Exception as e:
        return False, "", str(e)

def frontend_put(key, value):
    """Call Put on frontend"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        rpc_timeout = config.config['timeouts']['rpc_timeout']
        
        channel = grpc.insecure_channel(f'localhost:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.KeyValue(key=key, value=value, clientId=1, requestId=1)
        response = stub.Put(request, timeout=rpc_timeout)
        channel.close()
        
        if response.wrongLeader:
            return False, response.error
        return True, ""
    except Exception as e:
        return False, str(e)

def generate_test_data():
    """Generate random test data"""
    keys = []
    values = []
    
    for i in range(5):
        key = ''.join(random.choices(string.ascii_lowercase, k=5)) + str(i)
        value = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
        keys.append(key)
        values.append(value)
    
    return keys, values

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

    # Start a single server
    print("Starting server cluster...")
    success, error = call_start_raft(1)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"StartRaft(1) failed: {error}"
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Test ping
    if not ping_server(0):
        return TestResult(
            test_name, 5, test_max_points, "StartRaft succeeded but server 0 not responding to ping"
        )

    print("Server 0 responding to ping")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Server started successfully and responds to ping.",
    )

def test_server_basic_kv():
    """Test 3: Server Basic Key-Value Operations (20 points)"""
    print("\n=== Test: Server Basic Key-Value Operations ===")

    test_name = "Server Basic KV"
    test_max_points = 20

    # Test missing key returns empty string
    print("Testing GET on missing key...")
    success, key, value = server_get(0, "nonexistent_key")
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Server GET failed: {value}"
        )
    
    if value != "":
        return TestResult(
            test_name, 5, test_max_points, f"GET missing key should return empty string, got: '{value}'"
        )
    
    print("Missing key correctly returns empty string")

    # Test PUT and GET
    test_key = "test_key_123"
    test_value = "test_value_456"
    
    print(f"Testing PUT: {test_key} -> {test_value}")
    if not server_put(0, test_key, test_value):
        return TestResult(
            test_name, 10, test_max_points, "Server PUT operation failed"
        )
    
    print("Testing GET after PUT...")
    success, ret_key, ret_value = server_get(0, test_key)
    if not success:
        return TestResult(
            test_name, 15, test_max_points, f"Server GET after PUT failed: {ret_value}"
        )
    
    if ret_key != test_key or ret_value != test_value:
        return TestResult(
            test_name, 15, test_max_points, 
            f"GET returned wrong data. Expected ({test_key}, {test_value}), got ({ret_key}, {ret_value})"
        )

    print("PUT and GET working correctly")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Server correctly handles missing keys, PUT, and GET operations.",
    )

def test_server_multiple_operations():
    """Test 4: Server Multiple Operations (15 points)"""
    print("\n=== Test: Server Multiple Operations ===")

    test_name = "Server Multiple Ops"
    test_max_points = 15

    # Generate test data
    keys, values = generate_test_data()
    
    print(f"Testing multiple PUT operations with {len(keys)} key-value pairs...")
    
    # Store all key-value pairs
    for i, (key, value) in enumerate(zip(keys, values)):
        print(f"  PUT {i+1}/{len(keys)}: {key} -> {value}")
        if not server_put(0, key, value):
            return TestResult(
                test_name, i * 3, test_max_points, f"PUT failed for key: {key}"
            )
    
    print("All PUT operations succeeded, testing retrieval...")
    
    # Retrieve all key-value pairs
    for i, (key, expected_value) in enumerate(zip(keys, values)):
        success, ret_key, ret_value = server_get(0, key)
        if not success:
            points = test_max_points - 3
            return TestResult(
                test_name, points, test_max_points, f"GET failed for key: {key}"
            )
        
        if ret_key != key or ret_value != expected_value:
            points = test_max_points - 3
            return TestResult(
                test_name, points, test_max_points, 
                f"GET returned wrong data for {key}. Expected '{expected_value}', got '{ret_value}'"
            )
        
        print(f"  GET {i+1}/{len(keys)}: {key} -> {ret_value} ✓")
    
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"Server correctly handled {len(keys)} PUT/GET operations.",
    )

def test_frontend_connectivity():
    """Test 5: Frontend Connectivity and Server Discovery (10 points)"""
    print("\n=== Test: Frontend Connectivity ===")

    test_name = "Frontend Connectivity"
    test_max_points = 10

    # Test that frontend can reach servers
    print("Testing frontend connectivity to servers...")
    
    success, value, error = frontend_get("test_connectivity")
    if not success:
        # Check if it's a "no servers available" error vs other error
        if "server" in error.lower() or "connection" in error.lower():
            return TestResult(
                test_name, 5, test_max_points, f"Frontend cannot find servers: {error}"
            )
        else:
            return TestResult(
                test_name, 7, test_max_points, f"Frontend GET failed: {error}"
            )
    
    print("Frontend successfully connected to server")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Frontend successfully connects to and communicates with server.",
    )

def test_frontend_kv_operations():
    """Test 6: Frontend Key-Value Operations (20 points)"""
    print("\n=== Test: Frontend Key-Value Operations ===")

    test_name = "Frontend KV Ops"
    test_max_points = 20

    # Test GET missing key
    print("Testing frontend GET on missing key...")
    success, value, error = frontend_get("missing_key_frontend")
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"Frontend GET failed: {error}"
        )
    
    if value != "":
        return TestResult(
            test_name, 5, test_max_points, f"Frontend GET missing key should return empty string, got: '{value}'"
        )
    
    print("Frontend correctly handles missing keys")

    # Test PUT through frontend
    test_key = "frontend_test_key"
    test_value = "frontend_test_value"
    
    print(f"Testing frontend PUT: {test_key} -> {test_value}")
    success, error = frontend_put(test_key, test_value)
    if not success:
        return TestResult(
            test_name, 10, test_max_points, f"Frontend PUT failed: {error}"
        )
    
    # Test GET through frontend
    print("Testing frontend GET after PUT...")
    success, ret_value, error = frontend_get(test_key)
    if not success:
        return TestResult(
            test_name, 15, test_max_points, f"Frontend GET after PUT failed: {error}"
        )
    
    if ret_value != test_value:
        return TestResult(
            test_name, 15, test_max_points, 
            f"Frontend GET returned wrong value. Expected '{test_value}', got '{ret_value}'"
        )

    print("Frontend PUT and GET working correctly")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        "Frontend correctly handles PUT and GET operations.",
    )

def test_end_to_end_workflow():
    """Test 7: End-to-End Workflow (10 points)"""
    print("\n=== Test: End-to-End Workflow ===")

    test_name = "End-to-End Workflow"
    test_max_points = 10

    # Test a complete workflow using frontend
    print("Testing complete workflow through frontend...")
    
    # Generate some test data
    workflow_keys, workflow_values = generate_test_data()
    workflow_keys = workflow_keys[:3]  # Use first 3 items
    workflow_values = workflow_values[:3]
    
    # Store data via frontend
    for i, (key, value) in enumerate(zip(workflow_keys, workflow_values)):
        success, error = frontend_put(key, value)
        if not success:
            return TestResult(
                test_name, i * 3, test_max_points, f"Workflow PUT failed for {key}: {error}"
            )
        print(f"  Stored: {key} -> {value}")
    
    # Retrieve data via frontend  
    for i, (key, expected_value) in enumerate(zip(workflow_keys, workflow_values)):
        success, ret_value, error = frontend_get(key)
        if not success:
            points = test_max_points - 2
            return TestResult(
                test_name, points, test_max_points, f"Workflow GET failed for {key}: {error}"
            )
        
        if ret_value != expected_value:
            points = test_max_points - 2
            return TestResult(
                test_name, points, test_max_points, 
                f"Workflow GET wrong value for {key}. Expected '{expected_value}', got '{ret_value}'"
            )
        
        print(f"  Retrieved: {key} -> {ret_value}")
    
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"End-to-end workflow completed successfully with {len(workflow_keys)} operations.",
    )

def main():
    print("Assignment 2 Language-Agnostic Test Suite")
    print("Single-Server Key-Value Store")
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
    
    # Start frontend service automatically
    if not start_frontend():
        print("FATAL ERROR: Could not start frontend service")
        return
    
    print("\nFrontend service is ready, starting tests...\n")
    
    # Initialize test suite
    suite = TestSuite()
    
    try:
        # Run tests
        suite.add(test_config_file())
        suite.add(test_server_startup())
        suite.add(test_server_basic_kv())
        suite.add(test_server_multiple_operations())
        suite.add(test_frontend_connectivity())
        suite.add(test_frontend_kv_operations())
        suite.add(test_end_to_end_workflow())
    
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