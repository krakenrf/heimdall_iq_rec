# Import built-in libraries
import time
import socket
import sys
import os
import os.path
from os.path import join
import logging
from struct import *
import queue
from threading import Thread
import shutil
import curses

# Import third-party libraries
from sshkeyboard import listen_keyboard, stop_listening
import numpy as np
from configparser import ConfigParser

from iq_header import IQHeader

"""
    HeIMDALL-RTL
    IQ Streamer
    
    Compatible with IQ header version 6 or above
    Author: Tamás Pető
    Python version: 3.6    
    License: GNU GPL V3
        
   This program is free software: you can redistribute it and/or modify
   it under the terms of the GNU General Public License as published by
   the Free Software Foundation, either version 3 of the License, or
   any later version.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

key_control_queue = queue.Queue()

async def key_press(key):
    if key_control_queue.empty():
        key_control_queue.put(key)    
    
class IQStreamer:
    
    def __init__(self):
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
        
        # IQ Frame recording       
        self.recorded_frames = 0
        self.recorded_data_size = 0
        self.frame_size = 0
        self.en_save_iq = False # Enable to save iq samples
        self.check_frame_type=False # If enabled only data frames will be saved
        self.iq_record_path = ""
        self.fname_prefix   = ""
        self.progress_bar_length = 30
        
        # Streaming
        self.en_streaming  = False
        self.stream_fname = "stream_test"
        self.stream_descriptor = None
        
        self.receiver_connection_status = False
        self.port = 5000
        self.rec_ip_addr = "127.0.0.1"
        self.receiverBufferSize = 2 ** 18  # Size of the Ethernet receiver buffer measured in bytes        
        self.socket_inst = socket.socket()
        
        # Overwrite default configuration
        self._read_config_file("iq_rec_config.ini")
        
        self.iq_frame_bytes = None
        self.iq_samples = np.empty(0)
        self.iq_header = IQHeader()
        
        self.status_msg = "READY"
        self.received_frame_cntr = 0        
        self.status_update_cntr = 0
        
        # For dropped frames calculation
        self.dropped_frames = 0
        self.last_frame_index = 0
        
        self.dropped_cpis = 0
        self.last_cpi_index = 0        
        
    
    def _read_config_file(self, config_filename):
        """
            Configures the internal parameters of the module based 
            on the values set in the confiugration file.

            TODO: Handle configuration field read failure
            Parameters:
            -----------
                :param: config_filename: Name of the configuration file
                :type:  config_filename: string
                    
            Return values:
            --------------
                :return: 0: Confiugrations fields succesfully applied
                        -1: Configuration file not found
        """
        parser = ConfigParser()
        found = parser.read([config_filename])
        if not found:
            self.logger.error("Configuration file not found. Default parameters will be used!")
            return -1
        self.iq_record_path = parser.get('iq_record', 'iq_record_path')
        self.rec_ip_addr    = parser.get('iq_record', 'daq_ip_addr')
        self.port           = parser.getint('iq_record', 'daq_port')        
              
        return 0
    
    def main(self, stdscr):
        
        #stdscr = curses.initscr()
        
        # Initialize color
        curses.start_color()
        curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.cbreak()
        stdscr.keypad(1)
            
        las_status_update =0
        while True:             
            start_time = time.time()
            self.status_update_cntr +=1            
            ###########################
            #     Keyboard events     #
            ###########################
            if not key_control_queue.empty():                
                key = key_control_queue.get()
                
                # Exit command                
                if key == 'q':
                    break  # finishing the loop
                # Create Ethernet connection
                elif key == 'c':
                    if not self.receiver_connection_status:
                        if self.connect() == 0:
                            self.status_msg = "Connection established with " + self.rec_ip_addr                    
                        else:
                            self.status_msg = "Establishing connection failed with " + self.rec_ip_addr
                        las_status_update = self.status_update_cntr
                
                # Disconnect from IQ Server
                elif key == 'd':  
                    if self.close() == 0:
                        self.status_msg = "IQ server connection closed "
                    else:
                        self.status_msg = "Failed to close IQ server connection "                
                    las_status_update = self.status_update_cntr
                    
                # Enable IQ recording
                elif key == 'r':  
                    if not self.en_save_iq:
                        self.en_save_iq = True
                        self.status_msg = "Start recording"
                        self.recorded_frames = 0
                        
                        # Generate folder structure
                        # -> Automatic data set folder creation
                        all_subdirs = [join(self.iq_record_path,d) for d in os.listdir(self.iq_record_path) if os.path.isdir(join(self.iq_record_path,d))]
                        if len(all_subdirs):
                            latest_subdir    = max(all_subdirs, key=os.path.getmtime)
                            record_index     = int(os.path.basename(latest_subdir))
                        else:
                            record_index = -1
                            
                        record_id = "{:04d}".format(record_index+1)

                        base_path         = join(self.iq_record_path, record_id)
                        iq_path           = join(base_path, "iq")
                        res_path          = join(base_path, "results")
                        target_info_path  = join(base_path, "target_info")

                        os.makedirs(base_path) 
                        os.makedirs(iq_path) 
                        os.makedirs(res_path) 
                        os.makedirs(target_info_path)
                        
                        self.fname_prefix = iq_path
                
                # Disable IQ recording
                elif key == 't':  
                    if self.en_save_iq:
                        self.en_save_iq = False
                        self.status_msg = "Stop recording"
                    las_status_update = self.status_update_cntr
                
                # Start streaming
                elif key == 's':  
                    if not self.en_streaming:
                        self.en_streaming = True
                        self.status_msg = "Start streaming"
                        
                        # Open file descriptor for streaming
                        self.stream_descriptor=open(self.stream_fname, "wb")

                    las_status_update = self.status_update_cntr
                
                # Stop streaming
                elif key == 'x':  
                    if self.en_streaming:
                        self.en_streaming = False
                        self.status_msg = "Stop streaming"
                        
                        # Close stream file descriptor
                        self.stream_descriptor.close()

                    las_status_update = self.status_update_cntr
            
            else:
                if self.status_update_cntr > las_status_update+10: 
                    self.status_msg = "READY"
            
                
            ###########################
            #   Data Acquisition      #
            ###########################                
            if self.receiver_connection_status:                                        
                iq_samples = self.get_iq_online()                
                
                self.received_frame_cntr +=1 
                #self.iq_header.dump_header()                
            
                # Check IQ Frame drop
                self.last_cpi_index+=1
                if self.last_cpi_index != self.iq_header.cpi_index:
                    self.dropped_frames += self.iq_header.cpi_index - self.last_cpi_index
                    self.last_cpi_index=self.iq_header.cpi_index
                
                if self.check_frame_type:
                    if self.iq_header.frame_type == 0:
                        check_frame_flag = True 
                    else:
                        check_frame_flag = False
                else:
                    check_frame_flag = True

                if self.en_save_iq and check_frame_flag:
                    self.recorded_frames +=1
                    self.recorded_data_size += self.frame_size                         
                    self.save_ig(join(self.fname_prefix, "{:04d}".format(self.recorded_frames)))
                
                if self.en_streaming and check_frame_flag:
                    self.stream_descriptor.write(self.iq_frame_bytes)
                
            else:
                time.sleep(0.2)                        
            ###########################
            #      Status display     #
            ###########################     
            
            # -> Connection status color0
            if self.receiver_connection_status:
                conn_status_str = "Connected"
                conn_color=2
            else:
                conn_status_str = "Disconnected"                
                conn_color=1
            
            # -> Frame type
            if self.iq_header.frame_type == 0:
                frame_type_str = "Normal data"
                frame_type_color = 2
            elif self.iq_header.frame_type == 1:
                frame_type_str = "Dummy"
                frame_type_color = 3
            elif self.iq_header.frame_type == 3:
                frame_type_str = "Calibration"
                frame_type_color = 3
            elif self.iq_header.frame_type == 4:
                frame_type_str = "Trigw"
                frame_type_color = 3
            else:
                frame_type_str = "Unknown"            
                frame_type_color = 1
            
            # -> Check ADC overdrive            
            if self.iq_header.adc_overdrive_flags:
                overdrive_color = 1
                overdrive_status_str = "ADC OVERDRIVE!"
            else:
                overdrive_color = 2
                overdrive_status_str = "Ok"
                
            # -> Check delay sync
            if not self.iq_header.delay_sync_flag:  
                delay_sync_loss_color = 1
                delay_sync_str = "SYNC LOSS"
            else:
                delay_sync_loss_color = 2
                delay_sync_str = "Synced"
            
            # -> Check IQ sync
            if not self.iq_header.iq_sync_flag:
                iq_sync_loss_color = 1
                iq_sync_str = "SYNC LOSS"
            else:
                iq_sync_loss_color = 2
                iq_sync_str = "Ok"
            
            # -> Check noise source state
            if not self.iq_header.noise_source_state:
                noise_source_str = "Disabled"
                noise_source_color = 2
            else:
                noise_source_str = "Enabled"
                noise_source_color = 1
            
            # IQ streaming
            if self.en_streaming:
                streaming_str = "Enabled"
                streaming_color = 2
            else:
                streaming_str = "Disabled"
                streaming_color = 1
            
            # IQ Recording
            if self.en_save_iq:
                record_fname_str = join(self.fname_prefix,"{:04d}".format(self.recorded_frames))
                recording_str = "Enabled"
                recording_color = 2
            else:
                record_fname_str = "-"
                recording_str = "Disabled"
                recording_color = 1
            
            # Format recorded data size string
            if self.recorded_data_size < 1024:
                recorded_data_size_str = "{:d}".format(int(self.recorded_data_size))+" Byte"
            if self.recorded_data_size > 1024:
                recorded_data_size_str = "{:.2f}".format(self.recorded_data_size/1024)+" KByte"
            if self.recorded_data_size > (1024**2):
                recorded_data_size_str = "{:.2f}".format(self.recorded_data_size/(1024**2))+" MByte"
            if self.recorded_data_size > (1024**3):
                recorded_data_size_str = "{:.2f}".format(self.recorded_data_size/(1024**3))+" GByte"
                        
            # Disk space
            (total, used, free) = shutil.disk_usage(self.iq_record_path)
            
            # -> Progress bar
            disk_used_percent = int(used/total*100)            
            hashes =int(self.progress_bar_length*used/total)                        
            progress_bar = "["+"#"*hashes+" "*int(self.progress_bar_length-hashes)+"]"
            
            
            # Calculate update rate
            elapsed_time = (time.time()-start_time)*1000
            
            stdscr.clear()
            stdscr.addstr(0,  0,"--->HeIMDALL IQ Frame Streamer and Recorder<---")
            stdscr.addstr(1,  0,"Version: 1.0-230721")            
            stdscr.addstr(2,  0,"q   : exit program")
            stdscr.addstr(3,  0,"c/d : Connection/Disconnect IQ server")
            stdscr.addstr(4,  0,"s/x : Start/Stop streaming to file")
            stdscr.addstr(5,  0,"r/t : Enable/Disable IQ frame recording")
            stdscr.addstr(6,  0,"-----------------------------------------------")
            stdscr.addstr(8,  0, "Connection status:"+conn_status_str, curses.color_pair(conn_color))
            stdscr.addstr(9,  0, "Update rate: {:.2f} ms".format(elapsed_time))    
            stdscr.addstr(10, 0, "Received IQ frames:"+str(self.received_frame_cntr))
            stdscr.addstr(11, 0, "Frame type: "+frame_type_str, curses.color_pair(frame_type_color))
            stdscr.addstr(12, 0, "Total dropped IQ frames:"+str(self.dropped_frames))            
            stdscr.addstr(13, 0, "Signal power level:"+overdrive_status_str, curses.color_pair(overdrive_color))
            stdscr.addstr(14, 0, "Delay sync status:"+delay_sync_str, curses.color_pair(delay_sync_loss_color))
            stdscr.addstr(15, 0, "IQ sync status:"+iq_sync_str, curses.color_pair(iq_sync_loss_color))
            stdscr.addstr(16, 0, "Noise source:"+noise_source_str, curses.color_pair(noise_source_color))
            stdscr.addstr(17, 0, "Streaming:"+streaming_str, curses.color_pair(streaming_color))            
            stdscr.addstr(18, 0, "Recording :"+recording_str, curses.color_pair(recording_color))
            stdscr.addstr(19, 0, "Recording into :"+record_fname_str)
            stdscr.addstr(20, 0, "Recorded frames:"+str(self.recorded_frames))
            stdscr.addstr(21, 0, "Recorded data size:"+recorded_data_size_str)
            stdscr.addstr(22, 0, "Used space on disk:"+progress_bar+" {:d}%".format(disk_used_percent))                
            
            stdscr.addstr(24, 0, "Status: "+self.status_msg)
            
            stdscr.refresh()
        stdscr.keypad(0)
    def connect(self):        
        """
            Compatible only with DAQ firmwares that has the IQ streaming mode. 
            HR7 DAQ Firmware version: 0.2.1 or later
        """
        self.logger.info("Establishing connection..")
        try:
            self.socket_inst.connect((self.rec_ip_addr, self.port))            
            self.socket_inst.sendall(str.encode('streaming'))                                
            test_iq = self.receive_iq_frame()                        
            self.receiver_connection_status = True
            self.received_frame_cntr = 0
            self.dropped_frames = 0
            self.last_frame_index = self.iq_header.cpi_index        
            self.frame_size = int((self.iq_header.cpi_length * self.iq_header.active_ant_chs * (2*self.iq_header.sample_bit_depth))/8)
            self.dropped_cpis = 0
            self.last_cpi_index=self.iq_header.cpi_index
            
        except:
            errorMsg = sys.exc_info()[0]
            self.receiver_connection_status = False
            self.logger.error("Error message: "+str(errorMsg))
            self.logger.error("Unexpected error: {0}".format(sys.exc_info()[0]))
            return -1            
            
            #self.logger.info("Connection established")
        return 0
    
    def close(self):            
        if self.receiver_connection_status:
            self.socket_inst.sendall(str.encode('q'))  # Quit from the server
            try:
                self.socket_inst.close()  # close connection   
                self.receiver_connection_status = False
                self.socket_inst = socket.socket()
            except:
                errorMsg = sys.exc_info()[0]
                self.logger.error("Error message: "+str(errorMsg))
                self.logger.error("Unexpected error:", sys.exc_info()[0])            
                return -1                        
        return 0
    
    def get_iq_online(self):
    
        # Check connection
        if not self.receiver_connection_status:     
            self.logger.info("Connect before download")       
            fail = self.connect()
            if fail:
                return -1        
        self.socket_inst.sendall(str.encode("IQDownload")) # Send iq request command
        iq_samples = self.receive_iq_frame()
        return iq_samples
    
    def receive_iq_frame(self):
        """
                Receives IQ samples over Ethernet connection
        """
        total_received_bytes = 0
        recv_bytes_count = 0
        iq_header_bytes = bytearray(self.iq_header.header_size)  # allocate array
        view = memoryview(iq_header_bytes)  # Get buffer
        
        self.logger.debug("Starting IQ header reception")
        
        while total_received_bytes < self.iq_header.header_size:
            # Receive into buffer
            recv_bytes_count = self.socket_inst.recv_into(view, self.iq_header.header_size-total_received_bytes)
            view = view[recv_bytes_count:]  # reset memory region
            total_received_bytes += recv_bytes_count
        
        self.iq_header.decode_header(iq_header_bytes)
        self.logger.debug("IQ header received and decoded")
        #self.iq_header.dump_header()
        
        # Calculate total bytes to receive from the iq header data        
        total_bytes_to_receive = int((self.iq_header.cpi_length * self.iq_header.active_ant_chs * (2*self.iq_header.sample_bit_depth))/8)
        receiver_buffer_size = 2**18
        
        self.logger.debug("Total bytes to receive: {:d}".format(total_bytes_to_receive))
        
        total_received_bytes = 0
        recv_bytes_count = 0
        iq_data_bytes = bytearray(total_bytes_to_receive + receiver_buffer_size)  # allocate array
        view = memoryview(iq_data_bytes)  # Get buffer
        
        #self.logger.debug("Starting IQ reception")
        
        while total_received_bytes < total_bytes_to_receive:
            # Receive into buffer
            recv_bytes_count = self.socket_inst.recv_into(view, receiver_buffer_size)
            view = view[recv_bytes_count:]  # reset memory region
            total_received_bytes += recv_bytes_count
        
        self.logger.debug(" IQ data succesfully received")
        
        # Convert raw bytes to Complex float64 IQ samples
        self.iq_samples = np.frombuffer(iq_data_bytes[0:total_bytes_to_receive], dtype=np.complex64).reshape(self.iq_header.active_ant_chs, self.iq_header.cpi_length)
        
        self.iq_frame_bytes =  bytearray()+iq_header_bytes+iq_data_bytes[:total_bytes_to_receive]
        
        return self.iq_samples
    
    def save_ig(self, fname):
        """
            Store the current available iq samples into a file.
            The dataframe is stored in the file as a raw byte stream.
            IQ header first and then IQ data. The saved iq samples
            are in complex 64 format.
            
            Parameters:
            -----------
            :param: fname: Name of the IQ file
        """        
        iq_file_descr = open(fname+".iqf", "wb")
        iq_file_descr.write(self.iq_frame_bytes)
        iq_file_descr.close()

    

###############################
#
#           M A I N
#
###############################

def keyboard_task():
    listen_keyboard(on_press=key_press)    

thread = Thread(target = keyboard_task)
thread.start()

IQStreamer_inst0 = IQStreamer()
curses.wrapper(IQStreamer_inst0.main)
stop_listening()
thread.join()

# Set everything back to normal
curses.echo() 
curses.nocbreak()
curses.endwin()  # Terminate curses



