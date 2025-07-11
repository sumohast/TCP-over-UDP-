# TCP over UDP Implementation

A reliable, connection-oriented communication protocol implementation that provides TCP-like features over UDP transport layer.

## Overview

This project implements a custom TCP-like protocol over UDP, combining the speed of UDP with the reliability features of TCP. It includes connection establishment, reliable data transfer, flow control, and proper connection termination.

## Key Features

### 🔄 **Reliable Connection Management**
- Three-way handshake for connection establishment
- Four-way handshake for connection termination
- Complete TCP state machine implementation

### 📦 **Reliable Data Transfer**
- Sequence numbers for packet ordering
- Acknowledgment system with retransmission
- Duplicate packet detection and handling
- Out-of-order packet buffering

### ⚡ **Performance Optimizations**
- Sliding window protocol for flow control
- Fast retransmit on duplicate ACKs
- Multi-threaded sender/receiver architecture
- Configurable timeouts and window sizes

### 🛡️ **Error Handling**
- Automatic retransmission on packet loss
- Connection reset on critical errors
- Timeout-based connection management

## Protocol Components

### Packet Structure
```
| Source Port | Dest Port | Sequence # | ACK # | Flags | Data Length | Data |
|     2B      |    2B     |     4B     |  4B   |  2B   |     2B      |  *   |
```

### Connection States
- `CLOSED`, `LISTEN`, `SYN_SENT`, `SYN_RECEIVED`
- `ESTABLISHED`, `FIN_WAIT_1`, `FIN_WAIT_2`
- `CLOSE_WAIT`, `CLOSING`, `LAST_ACK`, `TIME_WAIT`

## Usage

### Server Mode
```bash
python tcp_over_udp.py server
```

### Client Mode
```bash
python tcp_over_udp.py
```

### Programmatic Usage
```python
# Server
server = Socket()
server.bind(('127.0.0.1', 8000))
server.listen(5)

conn, addr = server.accept()
data = conn.receive(1024)
conn.send(b"Response")
conn.close()

# Client
client = Socket()
client.connect(('127.0.0.1', 8000))
client.send(b"Hello")
response = client.receive(1024)
client.close()
```

## Configuration

```python
MSS = 1024                          # Maximum Segment Size
WINDOW_SIZE = 4096                  # Fixed window size
TIMEOUT = 5.0                       # Retransmission timeout
MAX_RETRIES = 3                     # Maximum retransmission attempts
FAST_RETRANSMIT_THRESHOLD = 3       # Duplicate ACKs for fast retransmit
```

## Architecture

### Multi-threaded Design
- **Listen Thread**: Handles incoming packets
- **Send Thread**: Manages outgoing data and retransmissions
- **Receive Thread**: Processes incoming data packets

### Flow Control
- Sliding window protocol implementation
- In-order delivery guarantee
- Buffering for out-of-order packets

## Requirements

- Python 3.6+
- No external dependencies (uses only standard library)

## Multi-threaded Server Example

For handling multiple concurrent clients, here's an enhanced server implementation:

```python
import threading
from tcp_over_udp import Socket

def handle_client(connection, client_addr):
    """Handle individual client connection"""
    try:
        print(f"New client connected: {client_addr}")
        while True:
            data = connection.receive(1024)
            if not data:
                break
            
            print(f"Received from {client_addr}: {data.decode()}")
            connection.send(f"Echo: {data.decode()}".encode())
            
    except Exception as e:
        print(f"Error with client {client_addr}: {e}")
    finally:
        print(f"Closing connection for {client_addr}")
        connection.close()

def run_multi_threaded_server():
    """Run server that handles multiple clients concurrently"""
    server = Socket()
    server.bind(('127.0.0.1', 8000))
    server.listen(10)  # Accept up to 10 concurrent connections
    
    print("Multi-threaded server listening on 127.0.0.1:8000")
    
    try:
        while True:
            conn, addr = server.accept()
            
            # Create new thread for each client
            client_thread = threading.Thread(
                target=handle_client, 
                args=(conn, addr),
                daemon=True
            )
            client_thread.start()
            
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.close()

if __name__ == "__main__":
    run_multi_threaded_server()
```

### Multiple Client Testing

Test with multiple clients simultaneously:

```bash
# Terminal 1 - Server
python tcp_over_udp.py server

# Terminal 2 - Client 1
python tcp_over_udp.py

# Terminal 3 - Client 2
python tcp_over_udp.py

# Terminal 4 - Client 3
python tcp_over_udp.py
```

## Demo

The included demo shows an echo server that:
1. Accepts multiple client connections
2. Echoes back received messages
3. Handles connection lifecycle properly
4. Supports concurrent client handling with threading

## Technical Details

### Handshake Process
```
Client                Server
  |                     |
  |-------SYN---------->|
  |                     |
  |<----SYN-ACK---------|
  |                     |
  |-------ACK---------->|
  |                     |
  |   ESTABLISHED       |
```

### Data Transfer
- Packets are segmented based on MSS
- Each segment gets a sequence number
- Receiver sends ACKs for received data
- Sender retransmits on timeout or duplicate ACKs

### Connection Termination
```
Client                Server
  |                     |
  |-------FIN---------->|
  |                     |
  |<------ACK-----------|
  |                     |
  |<------FIN-----------|
  |                     |
  |-------ACK---------->|
  |                     |
  |     CLOSED          |
```
