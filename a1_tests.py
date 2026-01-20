#!/usr/bin/env python3
"""
Assignment 1 Language-Agnostic Test Script
Tests any implementation that exposes the correct gRPC interface
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
        print(f"\n===== ASSIGNMENT 1 TEST RESULTS ({now}) =====")
        
        passed = 0
        for r in self.results:
            if r.score == r.max_points:
                status = "PASS"
                passed += 1
            elif r.score > 0:
                status = "PART"
            else:
                status = "FAIL"
            
            print(f"{r.name:<30} [{status}] {r.score:.1f}/{r.max_points:.1f} — {r.details}")
        
        total_tests = len(self.results)
        failed = total_tests - passed
        pass_rate = (passed / total_tests * 100) if total_tests > 0 else 0
        
        print(f"\nTests run: {total_tests}, Passed: {passed}, Failed: {failed} ({pass_rate:.1f}% pass rate)")
        print(f"Overall Score: {self.total:.1f}/100")
        print("=" * 50)

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
    
    # Generic cleanup by process pattern
    cleanup_all_processes()

def cleanup_server_processes_only():
    """Kill only server processes, not frontend"""
    if not config:
        return
    
    patterns = []
    
    # Add server pattern
    if 'process_name_pattern' in config.config['server']:
        patterns.append(config.config['server']['process_name_pattern'])
    
    # Add server command-based patterns
    server_cmd_template = config.config['server']['command_template']
    if len(server_cmd_template) > 0:
        # Remove the {server_id} placeholder for pattern matching
        server_pattern = server_cmd_template[-1].replace('{server_id}', '')
        if server_pattern:
            patterns.append(server_pattern)
    
    print(f"Cleaning up server processes matching patterns: {patterns}")
    
    for pattern in patterns:
        if pattern:
            try:
                # Try to kill by pattern
                subprocess.run(['pkill', '-f', pattern], 
                             capture_output=True, timeout=5)
                print(f"  Cleaned up server processes matching '{pattern}'")
            except:
                pass
    
    time.sleep(2)  # Allow processes to terminate

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
                # Try to kill by pattern
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
                
                # Should get "Not implemented" error, which means it's working
                if response.wrongLeader and "Not implemented" in response.error:
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
        
        # Show some debug info
        if frontend_process:
            try:
                stdout, stderr = frontend_process.communicate(timeout=1)
                if stdout:
                    print(f"Frontend stdout: {stdout.decode()[:500]}")
                if stderr:
                    print(f"Frontend stderr: {stderr.decode()[:500]}")
            except:
                pass
        
        return False
        
    except Exception as e:
        print(f"ERROR: Failed to start frontend service: {e}")
        return False

def check_frontend_running():
    """Check if frontend service is running"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        channel = grpc.insecure_channel(f'localhost:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        # Try a simple call to verify it's responding
        request = raft_pb2.GetKey(key="test", clientId=1, requestId=1)
        response = stub.Get(request, timeout=3)
        channel.close()
        
        # Should get "Not implemented" error, which means it's working
        return response.wrongLeader and "Not implemented" in response.error
    except Exception as e:
        print(f"Frontend check failed: {e}")
        return False

def call_start_raft(n):
    """Call StartRaft RPC with n servers"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        
        channel = grpc.insecure_channel(f'localhost:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.IntegerArg(arg=n)
        response = stub.StartRaft(request, timeout=30)  # Reasonable timeout for cluster startup
        channel.close()
        
        if response.error:
            return False, response.error
        return True, ""
    except Exception as e:
        return False, str(e)

def call_start_server(server_id):
    """Call StartServer RPC for specific server"""
    try:
        frontend_port = config.config['ports']['frontend_port']
        
        channel = grpc.insecure_channel(f'localhost:{frontend_port}')
        stub = raft_pb2_grpc.FrontEndStub(channel)
        
        request = raft_pb2.IntegerArg(arg=server_id)
        response = stub.StartServer(request, timeout=15)
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

def count_responsive_servers(max_servers):
    """Count how many servers are responsive"""
    responsive = []
    for i in range(max_servers):
        if ping_server(i):
            responsive.append(i)
    return responsive

def test_config_file():
    """Test 1: Configuration File"""
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
                    "The config file has all required sections and keys.",
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
                    f"The config file did not include: {missing}",
                )
        except Exception as e:
            return TestResult(
                test_name,
                file_existence_points,
                test_max_points,
                f"Cannot read the config file: {e}",
            )
    else:
        return TestResult(test_name, 0, test_max_points, f"The config file {filename_config} is missing.")

def test_frontend_service():
    """Test 2: Frontend Service Startup"""
    print("\n=== Test: Frontend Service Startup ===")

    test_name = "Frontend Service"
    test_max_points = 10

    if not check_frontend_running():
        return TestResult(
            test_name,
            0,
            test_max_points,
            "The frontend service is not responsive.",
        )

    frontend_port = config.config["ports"]["frontend_port"]
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"The frontend service is responsive on port {frontend_port}.",
    )
    
def test_start_raft_basic():
    """Test 3: StartRaft Basic Functionality"""
    print("\n=== Test: StartRaft Basic Functionality ===")

    test_name = "StartRaft Basic"
    test_max_points = 15
    test_servers = 3

    cleanup_server_processes_only()

    # Test starting servers
    print(f"Calling StartRaft({test_servers})...")
    success, error = call_start_raft(test_servers)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"StartRaft({test_servers}) RPC call failed: {error}."
        )

    print("StartRaft succeeded, waiting for servers...")
    time.sleep(config.config["timeouts"]["startup_wait"])

    # Check if servers are responding (most important test)
    responsive_servers = count_responsive_servers(test_servers)
    print(f"Servers responding to ping: {responsive_servers}")

    if len(responsive_servers) == test_servers:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            f"Successfully started {test_servers} servers. All servers are responsive.",
        )
    elif len(responsive_servers) > 0:
        points = int(test_max_points / test_servers * len(responsive_servers))
        return TestResult(
            test_name,
            points,
            test_max_points,
            f"Started {test_servers} servers but only {len(responsive_servers)}/{test_servers} are responsive.",
        )
    else:
        return TestResult(
            test_name, 0, test_max_points, "No servers are responsive."
        )

def test_server_connectivity():
    """Test 4: Server RPC Connectivity"""
    print("\n=== Test: Server RPC Connectivity ===")

    test_name = "Server RPC Connectivity"
    test_max_points = 20
    ping_points = 5
    test_servers = 3

    # Test ping functionality
    ping_results = count_responsive_servers(test_servers)
    print(f"Servers responsive to ping: {ping_results}")

    if len(ping_results) == 0:
        return TestResult(
            test_name, 0, test_max_points, "No servers are responsive to ping."
        )
    elif len(ping_results) < test_servers:
        partial_ping_points = int(ping_points / test_servers * len(ping_results))
        return TestResult(
            test_name,
            partial_ping_points,
            test_max_points,
            f"Only {len(ping_results)}/{test_servers} servers are responsive to ping.",
        )

    # Test GetState functionality
    potential_points = test_max_points - ping_points
    potential_points_each = potential_points / test_servers

    state_responses = 0
    print("Testing server GetState...")
    for i in range(test_servers):
        success, term, is_leader = get_server_state(i)
        if success:
            state_responses += 1
            print(f"  Server {i}: GetState OK (term={term}, leader={is_leader})")
        else:
            print(f"  Server {i}: GetState FAILED")

    if state_responses == test_servers:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            "All servers are responsive to ping and GetState.",
        )
    elif state_responses > 0:
        points = ping_points + int(potential_points_each * state_responses)
        return TestResult(
            test_name,
            points,
            test_max_points,
            f"Only {state_responses}/{test_servers} servers are responsive to GetState.",
        )
    else:
        return TestResult(
            test_name,
            ping_points,
            test_max_points,
            "Servers are responsive to ping but not GetState.",
        )

def test_start_raft_different_sizes():
    """Test 5: StartRaft with Different Cluster Sizes"""
    print("\n=== Test: StartRaft with Different Sizes ===")

    test_name = "StartRaft Different Size"
    test_max_points = 20
    test_servers = 5
    potential_points_each = test_max_points / test_servers

    # Test starting more servers
    cleanup_server_processes_only()
    print(f"Calling StartRaft({test_servers})...")
    success, error = call_start_raft(test_servers)
    if not success:
        return TestResult(
            test_name, 0, test_max_points, f"StartRaft({test_servers}) failed: {error}."
        )

    time.sleep(config.config["timeouts"]["startup_wait"])

    # Check if servers are responding
    responsive_servers = count_responsive_servers(test_servers)
    print(f"Servers responding to ping: {responsive_servers}")

    if len(responsive_servers) == test_servers:
        return TestResult(
            test_name,
            test_max_points,
            test_max_points,
            f"StartRaft({test_servers}) - All servers are responsive.",
        )
    elif len(responsive_servers) > 0:
        points = int(potential_points_each * len(responsive_servers))
        return TestResult(
            test_name,
            points,
            test_max_points,
            f"StartRaft({test_servers}) - Only {len(responsive_servers)}/{test_servers} servers are responsive.",
        )
    else:
        return TestResult(
            test_name,
            0,
            test_max_points,
            f"StartRaft({test_servers}) - No servers are responsive.",
        )

def test_start_server_individual():
    """Test 6: Individual Server Start/Restart"""
    print("\n=== Test: Individual Server Start/Restart ===")

    test_name = "Individual Server Start/Restart"
    test_max_points = 15
    test_servers = 3
    restart_timeout = 5

    print("Ensuring servers are responsive...")
    responsive_servers = count_responsive_servers(test_servers)

    if len(responsive_servers) < test_servers:
        print("Not all servers responsive, starting a new cluster...")
        cleanup_server_processes_only()
        success, error = call_start_raft(test_servers)
        if not success:
            return TestResult(
                test_name, 0, test_max_points, f"Failed to start a new cluster: {error}."
            )
        time.sleep(config.config["timeouts"]["startup_wait"])
        responsive_servers = count_responsive_servers(test_servers)

    if len(responsive_servers) < 2:
        return TestResult(
            test_name,
            0,
            test_max_points,
            "Cannot test individual server restart - requires at least 2 responsive servers.",
        )

    # Pick server 2 to restart if available, otherwise pick the last responsive server
    target_server = 2 if 2 in responsive_servers else responsive_servers[-1]

    print(f"Testing restart of server {target_server}...")

    # Restart server by calling StartServer RPC
    print(f"Restarting server {target_server} using StartServer RPC...")
    success, error = call_start_server(target_server)
    if not success:
        return TestResult(
            test_name,
            int(test_max_points / 3),
            test_max_points,
            f"Restart Server {target_server} - StartServer({target_server}) failed: {error}.",
        )

    time.sleep(restart_timeout)

    if not ping_server(target_server):
        return TestResult(
            test_name,
            int(test_max_points / 3 * 2),
            test_max_points,
            f"Restart Server {target_server} - StartServer({target_server}) succeeded but server not responsive.",
        )

    print(f"Server {target_server} successfully restarted!")
    return TestResult(
        test_name,
        test_max_points,
        test_max_points,
        f"Restart Server {target_server} succeeded.",
    )

def test_unimplemented_operations():
    """Test 7: Get/Put Return Not Implemented"""
    print("\n=== Test: Unimplemented Operations ===")

    test_name = "Unimplemented Ops"
    test_max_points = 10

    try:
        frontend_port = config.config["ports"]["frontend_port"]
        rpc_timeout = config.config["timeouts"]["rpc_timeout"]

        channel = grpc.insecure_channel(f"localhost:{frontend_port}")
        stub = raft_pb2_grpc.FrontEndStub(channel)

        get_request = raft_pb2.GetKey(key="test", clientId=1, requestId=1)
        get_response = stub.Get(get_request, timeout=rpc_timeout)

        put_request = raft_pb2.KeyValue(
            key="test", value="val", clientId=1, requestId=2
        )
        put_response = stub.Put(put_request, timeout=rpc_timeout)

        channel.close()

        get_ok = get_response.wrongLeader and "Not implemented" in get_response.error
        put_ok = put_response.wrongLeader and "Not implemented" in put_response.error

        if get_ok and put_ok:
            return TestResult(
                test_name,
                test_max_points,
                test_max_points,
                "Get & Put correctly returned 'Not implemented'.",
            )
        elif get_ok:
            return TestResult(
                test_name,
                int(test_max_points / 2),
                test_max_points,
                "Only Get correctly returned 'Not implemented'.",
            )
        elif put_ok:
            return TestResult(
                test_name,
                int(test_max_points / 2),
                test_max_points,
                "Only Put correctly returned 'Not implemented'.",
            )
        else:
            return TestResult(
                test_name,
                0,
                test_max_points,
                "Get & Put failed to return 'Not implemented'.",
            )
    except Exception as e:
        return TestResult(test_name, 0, test_max_points, f"RPC failed: {e}")

def main():
    print("Assignment 1 Language-Agnostic Test Suite")
    print("=" * 50)
    
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
        suite.add(test_frontend_service())
        suite.add(test_start_raft_basic())
        suite.add(test_server_connectivity())
        suite.add(test_start_raft_different_sizes())
        suite.add(test_start_server_individual())
        suite.add(test_unimplemented_operations())
    
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n\nUnexpected error during testing: {e}")
    finally:
        # Cleanup will be handled by atexit
        pass
    
    # Print results
    suite.print_results()

if __name__ == "__main__":
    main()