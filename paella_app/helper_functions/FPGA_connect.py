"""
FPGA Connection Module.

This module provides functions for establishing TCP connections to FPGA devices.
"""

import socket
import time


def initiate_fpga_connection(nios_ip='192.168.100.2', multicast_ip='224.1.1.1',
                            host_ip='192.168.100.1', udp_port=5007, remote_port=30):
    """
    Initiate a TCP connection to the FPGA device.
    
    Args:
        nios_ip: NIOS IP address (default: '192.168.100.2')
        multicast_ip: Multicast IP address (default: '224.1.1.1')
        host_ip: Host IP address (default: '192.168.100.1')
        udp_port: UDP port number (default: 5007)
        remote_port: Remote port number for TCP connection (default: 30)
    
    Returns:
        tuple: (success: bool, socket: socket.socket or None, response: bytes or None, error_message: str or None)
               On success, returns (True, socket_object, response_bytes, None)
               On failure, returns (False, None, None, error_message)
    """
    sock = None
    try:
        # Create TCP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)  # 5 second timeout
        
        # Connect to the FPGA device
        sock.connect((nios_ip, remote_port))
        
        # Wait a brief moment for connection to stabilize
        time.sleep(0.1)
        
        # Read 3 bytes from the connection
        response = sock.recv(3)
        
        if len(response) == 3:
            return True, sock, response, None
        else:
            sock.close()
            return False, None, None, f"Expected 3 bytes, received {len(response)} bytes"
            
    except socket.timeout:
        if sock:
            sock.close()
        return False, None, None, "Connection timeout"
    except socket.error as e:
        if sock:
            sock.close()
        return False, None, None, f"Socket error: {str(e)}"
    except Exception as e:
        if sock:
            sock.close()
        return False, None, None, f"Unexpected error: {str(e)}"


