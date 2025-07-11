import socket
import threading
import time
import random
import struct
import queue
from enum import Enum
from typing import Dict, Optional, Tuple, List
from collections import defaultdict

# Constants
MSS = 1024  # Maximum Segment Size
WINDOW_SIZE = 4096  # Fixed window size
TIMEOUT = 5.0  # Fixed timeout
MAX_RETRIES = 3
FAST_RETRANSMIT_THRESHOLD = 3  # Number of duplicate ACKs for fast retransmit

class PacketType(Enum):
    SYN = 0x01
    ACK = 0x02
    FIN = 0x04
    RST = 0x08
    SYN_ACK = 0x03
    FIN_ACK = 0x06

class ConnectionState(Enum):
    CLOSED = 0
    LISTEN = 1
    SYN_SENT = 2
    SYN_RECEIVED = 3
    ESTABLISHED = 4
    FIN_WAIT_1 = 5
    FIN_WAIT_2 = 6
    CLOSE_WAIT = 7
    CLOSING = 8
    LAST_ACK = 9
    TIME_WAIT = 10

class Packet:
    def __init__(self, src_port: int, dst_port: int, seq_num: int, ack_num: int, 
                 flags: int, data: bytes = b''):
        self.src_port = src_port
        self.dst_port = dst_port
        self.seq_num = seq_num
        self.ack_num = ack_num
        self.flags = flags
        self.data = data
        self.data_len = len(data)
    
    def serialize(self) -> bytes:
        """Convert packet to bytes for transmission"""
        header = struct.pack('!HHIIIH', 
                           self.src_port, self.dst_port, 
                           self.seq_num, self.ack_num, 
                           self.flags, self.data_len)
        return header + self.data
    
    # decorator for deserialization
    @classmethod
    def deserialize(cls, data: bytes) -> 'Packet':
        """Create packet from received bytes"""
        if len(data) < 18:  # Minimum header size
            raise ValueError("Invalid packet: too short")
        
        header = struct.unpack('!HHIIIH', data[:18])
        src_port, dst_port, seq_num, ack_num, flags, data_len = header
        
        if len(data) < 18 + data_len:
            raise ValueError("Invalid packet: data length mismatch")
        
        payload = data[18:18+data_len] if data_len > 0 else b''
        # make object as packet
        return cls(src_port, dst_port, seq_num, ack_num, flags, payload)
      

class Connection:
    def __init__(self, local_port: int, remote_addr: Tuple[str, int], 
                 udp_socket: socket.socket, initial_seq: int = None):
        self.local_port = local_port
        self.remote_addr = remote_addr
        self.udp_socket = udp_socket
        self.state = ConnectionState.CLOSED
        
        # Sequence numbers
        self.send_seq = initial_seq if initial_seq is not None else random.randint(1000, 10000)
        self.recv_seq = 0
        # acknowledgment number
        self.send_ack = 0
        
        # Buffers
        self.send_buffer = queue.Queue()
        self.recv_buffer = queue.Queue()  # For incoming packets
        self.recv_data_buffer = b'' # Buffer for received data
        
        # Window management
        self.send_window = {}  # {seq_num: (packet, timestamp, retries)}
        self.recv_window = {}  # {seq_num: packet}
        self.send_base = self.send_seq # Base sequence number for sending
        self.recv_base = 0 # Base sequence number for receiving
        
        # Fast retransmit
        self.duplicate_acks = defaultdict(int)  # {ack_num: count}
        self.last_ack_received = 0
        
        # Threading
        self.send_thread = None
        self.recv_thread = None
        self.running = False
        
        # Locks
        self.send_lock = threading.Lock()
        self.recv_lock = threading.Lock()
        self.state_lock = threading.Lock()

    def start_threads(self):
        """Start sender and receiver threads"""
        self.running = True
        self.send_thread = threading.Thread(target=self._send_worker, daemon=True)
        self.recv_thread = threading.Thread(target=self._recv_worker, daemon=True)
        self.send_thread.start()
        self.recv_thread.start()

    def stop_threads(self):
        """Stop all threads"""
        self.running = False
        if self.send_thread and self.send_thread.is_alive():
            self.send_thread.join(timeout=1.0)
        if self.recv_thread and self.recv_thread.is_alive():
            self.recv_thread.join(timeout=1.0)

    def _send_worker(self):
        """Thread worker for handling outgoing packets"""
        while self.running:
            try:
                # Check for data to send
                if not self.send_buffer.empty():
                    try:
                        data = self.send_buffer.get_nowait()
                        self._send_data_segments(data)
                    except queue.Empty:
                        pass
                
                # Check for retransmissions
                self._check_retransmissions()
                
                time.sleep(0.01)
            except Exception as e:
                print(f"Send worker error: {e}")
                break

    def _recv_worker(self):
        """Thread worker for handling incoming packets"""
        while self.running:
            try:
                if not self.recv_buffer.empty():
                    try:
                        packet = self.recv_buffer.get_nowait()
                        self._process_received_packet(packet)
                    except queue.Empty:
                        pass
                
                time.sleep(0.01)
            except Exception as e:
                print(f"Recv worker error: {e}")
                break

    def _send_data_segments(self, data: bytes):
        """Split data into segments and send"""
        with self.send_lock:
            offset = 0
            while offset < len(data):
                segment_size = min(MSS, len(data) - offset)
                segment_data = data[offset:offset + segment_size]
                
                # Simple flow control - don't send if window is full
                if len(self.send_window) >= WINDOW_SIZE // MSS:
                    time.sleep(0.01)
                    continue
                
                packet = Packet(
                    src_port=self.local_port,
                    dst_port=self.remote_addr[1],
                    seq_num=self.send_seq,
                    ack_num=self.send_ack,
                    flags=PacketType.ACK.value,
                    data=segment_data
                )
                # send packet to window
                self._send_packet(packet)
                self.send_window[self.send_seq] = (packet, time.time(), 0)
                self.send_seq += len(segment_data)
                offset += segment_size

    def _send_packet(self, packet: Packet):
        """Send a single packet"""
        try:
            self.udp_socket.sendto(packet.serialize(), self.remote_addr)
        except Exception as e:
            print(f"Error sending packet: {e}")

    def _check_retransmissions(self):
        """Check for packets that need retransmission"""
        with self.send_lock:
            current_time = time.time()
            to_retransmit = []
            
            for seq_num, (packet, timestamp, retries) in self.send_window.items():
                if current_time - timestamp > TIMEOUT:
                    if retries < MAX_RETRIES:
                        to_retransmit.append(seq_num)
                    else:
                        # Max retries reached, close connection
                        self._reset_connection()
                        return
                    
            #send packets that need retransmission. 
            for seq_num in to_retransmit: 
                if seq_num in self.send_window:
                    packet, _, retries = self.send_window[seq_num]
                    self._send_packet(packet)
                    # update the send window with new timestamp and increment retries. 
                    self.send_window[seq_num] = (packet, current_time, retries + 1)

    def _reset_connection(self):
        """Reset connection due to timeout"""
        with self.state_lock:
            self.state = ConnectionState.CLOSED
            self.stop_threads()

    def add_incoming_packet(self, packet: Packet):
        """Add incoming packet to receive buffer"""
        self.recv_buffer.put(packet)

    def _process_received_packet(self, packet: Packet):
        """Process packet in receive thread"""
        with self.state_lock:
            if self.state == ConnectionState.ESTABLISHED:
                self._handle_established_packet(packet)
            elif self.state == ConnectionState.SYN_SENT:
                self._handle_syn_sent_packet(packet)
            elif self.state == ConnectionState.SYN_RECEIVED:
                self._handle_syn_received_packet(packet)
            elif self.state == ConnectionState.FIN_WAIT_1:
                self._handle_fin_wait1_packet(packet)
            elif self.state == ConnectionState.CLOSE_WAIT:
                self._handle_close_wait_packet(packet)
            elif self.state == ConnectionState.LAST_ACK:
                self._handle_last_ack_packet(packet)
            elif self.state == ConnectionState.FIN_WAIT_2:
                self._handle_fin_wait2_packet(packet)
            else:
                # Invalid state, send RST
                self._send_rst_packet(packet)

    def _handle_established_packet(self, packet: Packet):
        """Handle packet in ESTABLISHED state"""
        # Validate packet
        if not self._is_valid_packet(packet):
            self._send_rst_packet(packet)
            return
        
        # Handle ACK packets
        if packet.flags & PacketType.ACK.value:
            self._process_ack(packet.ack_num)
        
        # Handle data packets
        if packet.data:
            self._process_data_packet(packet)
        
        # Handle FIN packets
        if packet.flags & PacketType.FIN.value:
            self.state = ConnectionState.CLOSE_WAIT
            self.recv_seq = packet.seq_num + 1
            self.send_ack = packet.seq_num + 1
            self._send_ack(self.send_ack)

    def _handle_syn_sent_packet(self, packet: Packet):
        """Handle packet in SYN_SENT state"""
        if packet.flags & PacketType.SYN.value and packet.flags & PacketType.ACK.value:
            self.recv_seq = packet.seq_num + 1
            self.send_ack = packet.seq_num + 1
            self.recv_base = self.recv_seq
            self._process_ack(packet.ack_num)
            self.state = ConnectionState.ESTABLISHED
            self._send_ack(self.send_ack)
            self.start_threads()
        else:
            self._send_rst_packet(packet)

    def _handle_syn_received_packet(self, packet: Packet):
        """Handle packet in SYN_RECEIVED state"""
        if packet.flags & PacketType.ACK.value:
            self._process_ack(packet.ack_num)
            self.state = ConnectionState.ESTABLISHED
            self.start_threads()
        else:
            self._send_rst_packet(packet)

    def _handle_fin_wait1_packet(self, packet: Packet):
        """Handle packet in FIN_WAIT_1 state"""
        if packet.flags & PacketType.ACK.value:
            self.state = ConnectionState.FIN_WAIT_2
        if packet.flags & PacketType.FIN.value:
            self.recv_seq = packet.seq_num + 1
            self.send_ack = packet.seq_num + 1
            self._send_ack(self.send_ack)
            if self.state == ConnectionState.FIN_WAIT_2:
                self.state = ConnectionState.TIME_WAIT
            else:
                self.state = ConnectionState.CLOSING

    def _handle_fin_wait2_packet(self, packet: Packet):
        """Handle packet in FIN_WAIT_2 state"""
        if packet.flags & PacketType.FIN.value:
            self.recv_seq = packet.seq_num + 1
            self.send_ack = packet.seq_num + 1
            self._send_ack(self.send_ack)
            self.state = ConnectionState.TIME_WAIT

    def _handle_close_wait_packet(self, packet: Packet):
        """Handle packet in CLOSE_WAIT state"""
        if packet.flags & PacketType.ACK.value:
            self._process_ack(packet.ack_num)

    def _handle_last_ack_packet(self, packet: Packet):
        """Handle packet in LAST_ACK state"""
        if packet.flags & PacketType.ACK.value:
            self.state = ConnectionState.CLOSED
            self.stop_threads()

    def _is_valid_packet(self, packet: Packet) -> bool:
        """Check if packet is valid for current connection state"""
        # Basic validation
        if packet.src_port != self.remote_addr[1]:
            return False
        if packet.dst_port != self.local_port:
            return False
        
        # Sequence number validation for data packets
        if packet.data and packet.seq_num < self.recv_base:
            return False
        
        return True

    def _process_ack(self, ack_num: int):
        """Process ACK packet with fast retransmit support"""
        with self.send_lock:
            # Check for duplicate ACKs
            if ack_num == self.last_ack_received:
                self.duplicate_acks[ack_num] += 1
                
                # Fast retransmit on 3 duplicate ACKs
                if self.duplicate_acks[ack_num] >= FAST_RETRANSMIT_THRESHOLD:
                    self._fast_retransmit(ack_num)
                    self.duplicate_acks[ack_num] = 0
            else:
                # New ACK received
                self.last_ack_received = ack_num
                self.duplicate_acks.clear()
                
                # Remove acknowledged packets from window
                acked_seqs = []
                for seq_num in list(self.send_window.keys()):
                    if seq_num < ack_num:
                        acked_seqs.append(seq_num)
                
                for seq_num in acked_seqs:
                    if seq_num in self.send_window:
                        del self.send_window[seq_num]
                
                if acked_seqs:
                    self.send_base = max(self.send_base, ack_num)

    def _fast_retransmit(self, ack_num: int):
        """Perform fast retransmit for the next packet after ack_num"""
        # Find the packet to retransmit
        min_seq = float('inf')
        packet_to_retransmit = None
        
        for seq_num, (packet, timestamp, retries) in self.send_window.items():
            if seq_num >= ack_num and seq_num < min_seq:
                min_seq = seq_num
                packet_to_retransmit = (packet, timestamp, retries)
        
        if packet_to_retransmit:
            packet, _, retries = packet_to_retransmit
            self._send_packet(packet)
            self.send_window[min_seq] = (packet, time.time(), retries + 1)

    def _process_data_packet(self, packet: Packet):
        """Process data packet"""
        with self.recv_lock:
            expected_seq = self.recv_base
            
            if packet.seq_num == expected_seq:
                # In-order packet
                self.recv_data_buffer += packet.data
                self.recv_base += len(packet.data)
                
                # Check if we have buffered out-of-order packets
                while self.recv_base in self.recv_window:
                    buffered_packet = self.recv_window[self.recv_base]
                    self.recv_data_buffer += buffered_packet.data
                    old_base = self.recv_base
                    self.recv_base += len(buffered_packet.data)
                    del self.recv_window[old_base]
                
                self.send_ack = self.recv_base
                self._send_ack(self.send_ack)
            
            elif packet.seq_num > expected_seq:
                # Out-of-order packet - buffer it
                self.recv_window[packet.seq_num] = packet
                self._send_ack(self.send_ack)  # Send current ACK
            
            else:
                # Duplicate packet - send current ACK
                self._send_ack(self.send_ack)

    def _send_ack(self, ack_num: int):
        """Send ACK packet"""
        packet = Packet(
            src_port=self.local_port,
            dst_port=self.remote_addr[1],
            seq_num=self.send_seq,
            ack_num=ack_num,
            flags=PacketType.ACK.value
        )
        self._send_packet(packet)

    def _send_rst_packet(self, original_packet: Packet):
        """Send RST packet"""
        rst_packet = Packet(
            src_port=self.local_port,
            dst_port=self.remote_addr[1],
            seq_num=original_packet.ack_num,
            ack_num=original_packet.seq_num + 1,
            flags=PacketType.RST.value
        )
        self._send_packet(rst_packet)

    def send(self, data: bytes):
        """Send data (add to send buffer)"""
        if self.state != ConnectionState.ESTABLISHED:
            raise ConnectionError("Connection not established")
        
        self.send_buffer.put(data)

    def receive(self, size: int) -> bytes:
        """Receive data from buffer"""
        if self.state not in [ConnectionState.ESTABLISHED, ConnectionState.CLOSE_WAIT]:
            raise ConnectionError("Connection not established")
        
        # Wait for data
        timeout = time.time() + TIMEOUT
        while len(self.recv_data_buffer) < size and time.time() < timeout:
            if not self.running and self.state not in [ConnectionState.ESTABLISHED, ConnectionState.CLOSE_WAIT]:
                break
            time.sleep(0.01)
        
        with self.recv_lock:
            if len(self.recv_data_buffer) >= size:
                data = self.recv_data_buffer[:size]
                self.recv_data_buffer = self.recv_data_buffer[size:]
                return data
            else:
                # Return whatever we have
                data = self.recv_data_buffer
                self.recv_data_buffer = b''
                return data

    def close(self):
        """Close connection"""
        with self.state_lock:
            if self.state == ConnectionState.ESTABLISHED:
                self.state = ConnectionState.FIN_WAIT_1
                fin_packet = Packet(
                    src_port=self.local_port,
                    dst_port=self.remote_addr[1],
                    seq_num=self.send_seq,
                    ack_num=self.send_ack,
                    flags=PacketType.FIN.value
                )
                self._send_packet(fin_packet)
                self.send_seq += 1
            elif self.state == ConnectionState.CLOSE_WAIT:
                self.state = ConnectionState.LAST_ACK
                fin_packet = Packet(
                    src_port=self.local_port,
                    dst_port=self.remote_addr[1],
                    seq_num=self.send_seq,
                    ack_num=self.send_ack,
                    flags=PacketType.FIN.value
                )
                self._send_packet(fin_packet)
                self.send_seq += 1
        
        # Wait for proper close
        timeout = time.time() + TIMEOUT
        while self.state not in [ConnectionState.CLOSED, ConnectionState.TIME_WAIT] and time.time() < timeout:
            time.sleep(0.01)
        
        self.stop_threads()

class Socket:
    def __init__(self):
        # AF_INET for IPv4, SOCK_DGRAM for UDP
        self.udp_socket = socket.socket(socket.AF_INET , socket.SOCK_DGRAM)
        # non-blocking mode with timeout.
        self.udp_socket.settimeout(0.1)
        self.local_addr = None
        self.connections: Dict[Tuple[str, int], Connection] = {}
        self.accept_queue = queue.Queue()
        self.listen_thread = None
        self.listening = False
        self.backlog = 0
        self.client_connection = None
        self.connection_lock = threading.Lock()
        self.closed = False

    def bind(self, address: Tuple[str, int]):
        """Bind socket to address"""
        self.udp_socket.bind(address)
        self.local_addr = self.udp_socket.getsockname()

    def listen(self, backlog: int = 5):
        """Start listening for connections"""
        if not self.local_addr:
            raise RuntimeError("Socket not bound")
        
        self.backlog = backlog
        self.listening = True
        self.listen_thread = threading.Thread(target=self._listen_worker, daemon=True)
        self.listen_thread.start()

    def _listen_worker(self):
        """Worker thread for listening to incoming packets"""
        while self.listening and not self.closed:
            try:
                data, addr = self.udp_socket.recvfrom(2048)
                # log the received packet
                print(f"[{'Server' if self.backlog > 0 else 'Client'}] Received a packet from {addr}")

                packet = Packet.deserialize(data)
                # log the packet details
                print(f"[{'Server' if self.backlog > 0 else 'Client'}] Packet details: Flags={packet.flags}, Seq={packet.seq_num}, Ack={packet.ack_num}")

                self._handle_incoming_packet(packet, addr)

            except socket.timeout:
                continue
            except Exception as e:
                if self.listening and not self.closed:
                    print(f"Listen worker error: {e}")
                break

    def _handle_incoming_packet(self, packet: Packet, addr: Tuple[str, int]):
        """Handle incoming packet"""
        with self.connection_lock:
            target_conn = None
            # Check if this is for an existing connection
            if addr in self.connections:
                target_conn = self.connections[addr]
            elif self.client_connection and addr == self.client_connection.remote_addr:
                target_conn = self.client_connection
                
            if target_conn:
                handshake_state = [ConnectionState.SYN_SENT, ConnectionState.SYN_RECEIVED]
                if target_conn.state in handshake_state:
                    target_conn._process_received_packet(packet)
                else :
                    target_conn.add_incoming_packet(packet)
                return
    
            
            # Handle new connection request
            if packet.flags & PacketType.SYN.value and not (packet.flags & PacketType.ACK.value):
                self._handle_new_connection(packet, addr)
            else:
                # Send RST for invalid packets
                self._send_rst(packet, addr)

    def _handle_new_connection(self, packet: Packet, addr: Tuple[str, int]):
        """Handle new connection request"""
        if self.accept_queue.qsize() >= self.backlog:
            return
        
        # Create new connection
        conn = Connection(
            local_port=self.local_addr[1],
            remote_addr=addr,
            udp_socket=self.udp_socket
        )
        
        conn.state = ConnectionState.SYN_RECEIVED
        conn.recv_seq = packet.seq_num + 1
        conn.send_ack = packet.seq_num + 1
        conn.recv_base = conn.recv_seq
        
        # Send SYN-ACK
        syn_ack = Packet(
            src_port=self.local_addr[1],
            dst_port=addr[1],
            seq_num=conn.send_seq,
            ack_num=conn.send_ack,
            flags=PacketType.SYN_ACK.value
        )
        
        self.udp_socket.sendto(syn_ack.serialize(), addr)
        conn.send_seq += 1
        
        # Add to connections and accept queue
        self.connections[addr] = conn
        self.accept_queue.put((conn, addr))

    def _send_rst(self, packet: Packet, addr: Tuple[str, int]):
        """Send RST packet"""
        try:
            rst_packet = Packet(
                src_port=self.local_addr[1] if self.local_addr else packet.dst_port,
                dst_port=packet.src_port,
                seq_num=packet.ack_num,
                ack_num=packet.seq_num + 1,
                flags=PacketType.RST.value
            )
            self.udp_socket.sendto(rst_packet.serialize(), addr)
        except Exception as e:
            print(f"Error sending RST: {e}")

    def accept(self) -> Tuple[Connection, Tuple[str, int]]:
        """Accept incoming connection"""
        if not self.listening:
            raise RuntimeError("Socket not listening")
        
        # Block until connection available
        timeout = time.time() + TIMEOUT
        while self.accept_queue.empty() and time.time() < timeout:
            time.sleep(0.01)
        
        if self.accept_queue.empty():
            raise ConnectionError("Accept timeout")
        
        conn, addr = self.accept_queue.get()
        
        # Wait for final ACK
        timeout = time.time() + TIMEOUT
        while conn.state != ConnectionState.ESTABLISHED and time.time() < timeout:
            time.sleep(0.01)
        
        if conn.state != ConnectionState.ESTABLISHED:
            # Connection failed
            with self.connection_lock:
                if addr in self.connections:
                    del self.connections[addr]
            raise ConnectionError("Connection establishment failed")
        
        return conn, addr

    def connect(self, address: Tuple[str, int]):
        """Connect to remote host"""
        if not self.local_addr:
            self.bind(('', 0))
        
        # Create connection
        self.client_connection = Connection(
            local_port=self.local_addr[1],
            remote_addr=address,
            udp_socket=self.udp_socket
        )
        
        # Start listening for responses
        if not self.listening:
            self.listening = True
            self.listen_thread = threading.Thread(target=self._listen_worker, daemon=True)
            self.listen_thread.start()
        
        # Send SYN
        syn_packet = Packet(
            src_port=self.local_addr[1],
            dst_port=address[1],
            seq_num=self.client_connection.send_seq,
            ack_num=0,
            flags=PacketType.SYN.value
        )
        
        self.client_connection.state = ConnectionState.SYN_SENT
        # log the SYN packet
        print(f"[Client] Sending SYN to {address}...") 

        self.udp_socket.sendto(syn_packet.serialize(), address)
        self.client_connection.send_seq += 1
        
        # Wait for connection establishment
        timeout = time.time() + TIMEOUT
        # log the waiting for SYN-ACK
        print("[Client] Waiting for SYN-ACK from server...") 

        while self.client_connection.state != ConnectionState.ESTABLISHED and time.time() < timeout:
            time.sleep(0.01)
        
        if self.client_connection.state != ConnectionState.ESTABLISHED:
            # log the timeout
            print("[Client] Timed out. Did not receive SYN-ACK.") 
            raise ConnectionError("Connection failed")
        else : 
            # log the successful connection
            print("[Client] Connection established.")

    def send(self, data: bytes):
        """Send data (client mode)"""
        if not self.client_connection:
            raise RuntimeError("Not connected")
        self.client_connection.send(data)

    def receive(self, size: int) -> bytes:
        """Receive data (client mode)"""
        if not self.client_connection:
            raise RuntimeError("Not connected")
        return self.client_connection.receive(size)

    def close(self):
        """Close socket"""
        self.closed = True
        self.listening = False
        
        # For server sockets: close connections in accept queue but keep accepted ones
        if hasattr(self, 'accept_queue'):
            while not self.accept_queue.empty():
                try:
                    conn, addr = self.accept_queue.get_nowait()
                    # Send FIN to connections in accept queue
                    fin_packet = Packet(
                        src_port=self.local_addr[1],
                        dst_port=addr[1],
                        seq_num=conn.send_seq,
                        ack_num=conn.send_ack,
                        flags=PacketType.FIN.value
                    )
                    self.udp_socket.sendto(fin_packet.serialize(), addr)
                    conn.close()
                except queue.Empty:
                    break
        
        # Close client connection
        if self.client_connection:
            self.client_connection.close()
            self.client_connection = None
        
        # Close UDP socket
        try:
            self.udp_socket.close()
        except:
            pass
        
        # Wait for threads to finish
        if self.listen_thread and self.listen_thread.is_alive():
            self.listen_thread.join(timeout=1.0)


# Example usage

if __name__ == "__main__":
    import sys
    def _send_ack(self, ack_num: int):

        """Send ACK packet"""

        packet = Packet(

            src_port=self.local_port,

            dst_port=self.remote_addr[1],

            seq_num=self.send_seq,

            ack_num=ack_num,

            flags=PacketType.ACK.value

        )

        self._send_packet(packet)



    def _send_rst_packet(self, original_packet: Packet):

        """Send RST packet"""

        rst_packet = Packet(

            src_port=self.local_port,

            dst_port=self.remote_addr[1],

            seq_num=original_packet.ack_num,

            ack_num=original_packet.seq_num + 1,

            flags=PacketType.RST.value

        )

        self._send_packet(rst_packet)



    def send(self, data: bytes):

        """Send data (add to send buffer)"""

        if self.state != ConnectionState.ESTABLISHED:

            raise ConnectionError("Connection not established")

        

        self.send_buffer.put(data)



    def receive(self, size: int) -> bytes:

        """Receive data from buffer"""

        if self.state not in [ConnectionState.ESTABLISHED, ConnectionState.CLOSE_WAIT]:

            raise ConnectionError("Connection not established")

        

        # Wait for data

        timeout = time.time() + TIMEOUT

        while len(self.recv_data_buffer) < size and time.time() < timeout:

            if not self.running and self.state not in [ConnectionState.ESTABLISHED, ConnectionState.CLOSE_WAIT]:

                break

            time.sleep(0.01)

        

        with self.recv_lock:

            if len(self.recv_data_buffer) >= size:

                data = self.recv_data_buffer[:size]

                self.recv_data_buffer = self.recv_data_buffer[size:]

                return data

            else:

                # Return whatever we have

                data = self.recv_data_buffer

                self.recv_data_buffer = b''

                return data



    def close(self):

        """Close connection"""

        with self.state_lock:

            if self.state == ConnectionState.ESTABLISHED:

                self.state = ConnectionState.FIN_WAIT_1

                fin_packet = Packet(

                    src_port=self.local_port,

                    dst_port=self.remote_addr[1],

                    seq_num=self.send_seq,

                    ack_num=self.send_ack,

                    flags=PacketType.FIN.value

                )

                self._send_packet(fin_packet)

                self.send_seq += 1

            elif self.state == ConnectionState.CLOSE_WAIT:

                self.state = ConnectionState.LAST_ACK

                fin_packet = Packet(

                    src_port=self.local_port,

                    dst_port=self.remote_addr[1],

                    seq_num=self.send_seq,

                    ack_num=self.send_ack,

                    flags=PacketType.FIN.value

                )

                self._send_packet(fin_packet)

                self.send_seq += 1

        

        # Wait for proper close

        timeout = time.time() + TIMEOUT

        while self.state not in [ConnectionState.CLOSED, ConnectionState.TIME_WAIT] and time.time() < timeout:

            time.sleep(0.01)

        

        self.stop_threads()


    if len(sys.argv) > 1 and sys.argv[1] == "server":
        # ======== server-side ========
        print("Starting server...")
        server_socket = Socket()
        server_socket.bind(('127.0.0.1', 8000))
        server_socket.listen(5)
        
        print("Server listening on 127.0.0.1:8000. Press Ctrl+C to stop.")

        
        def handle_client(connection, client_addr):
            try:
                while True:
                    data = connection.receive(1024)
                    if not data:
                        print(f"Connection from {client_addr} closed by peer.")
                        break 
                    
                    print(f"Received from {client_addr}: {data.decode()}")
                    connection.send(f"Echo: {data.decode()}".encode())
            except Exception as e:
                print(f"Error with client {client_addr}: {e}")
            finally:
                print(f"Closing connection for {client_addr}.")
                connection.close()

        try:
            
            while True:
                try:
                    conn, addr = server_socket.accept()
                    print(f"Accepted connection from {addr}")

                    client_thread = threading.Thread(target=handle_client, args=(conn, addr))
                    client_thread.daemon = True
                    client_thread.start()

                except ConnectionError:
                    continue
        
        except KeyboardInterrupt:
            print("\nShutting down server...")
        finally:
            server_socket.close()

    else:
        # ======== client-side ========
        print("Starting client...")
        client_socket = Socket()

        try:
            client_socket.connect(('127.0.0.1', 8000))
            print("Connected to server")
            messages = ["Hello", "World", "TCP over UDP", "Test" ,"mohast" , "project-network" , "devnull"]
            for msg in messages:
                client_socket.send(msg.encode())
                response = client_socket.receive(1024)
                print(f"Server response: {response.decode()}")
                time.sleep(1)

        except Exception as e:
            print(f"Client error: {e}")
            
        finally:
            client_socket.close()




        