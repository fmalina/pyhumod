#
# Copyright 2009 by Slawek Ligus <root@ooz.ie>
#
# Please refer to the LICENSE file for conditions 
#  under which this software may be distributed.
#
#   Visit http://huawei.ooz.ie/ for more info.
#

"""This module defines the base Modem() class."""

__author__ = 'Slawek Ligus <root@ooz.ie>'

import serial
import threading
import Queue
import time
import os
from humod import at_commands
from humod import errors
from humod import actions

DEFAULT_DATA_PORT = '/dev/ttyUSB0'
DEFAULT_CONTROL_PORT = '/dev/ttyUSB1'
PROBER_TIMEOUT = 0.5
PPPD_PATH = '/usr/sbin/pppd'

class Interpreter(threading.Thread):
    """Interpreter thread."""
    def __init__(self, queue, modem, patterns):
        self.active = True
        self.queue = queue
        self.patterns = patterns
        self.modem = modem
        threading.Thread.__init__(self)

    def run(self):
        """Keep interpreting messages while active attribute is set."""
        while self.active:
            self.interpret(self.queue.get())

    def interpret(self, message):
        """Match message pattern with action to take.

        Arguments:
            message -- string received from the modem.
        """
        for pattern_action in self.patterns:
            pattern, action = pattern_action
            if pattern.search(message):
                action(self.modem, message)
                break
        else:
            actions.no_match(self.modem, message)
            

class QueueFeeder(threading.Thread):
    """Queue feeder thread."""
    def __init__(self, queue, ctrl_port, ctrl_lock):
        self.active = True
        self.queue = queue
        self.ctrl_port = ctrl_port
        self.ctrl_lock = ctrl_lock
        threading.Thread.__init__(self)

    def run(self):
        """Start the feeder thread."""
        while self.active:
            self.ctrl_lock.acquire()
            try:
                # set timeout
                input_line = self.ctrl_port.readline() 
                self.queue.put(input_line)
            finally:
                self.ctrl_lock.release()
                # Putting the thread on idle between releasing
                # and acquiring the lock for 100ms
                time.sleep(.1)

    def stop(self):
        """Stop the queue feeder thread."""
        self.active = False
        self.ctrl_port.write('\r\n')


class Prober(object):
    """Class responsible for reading in and queueing of control data."""

    def __init__(self, modem):
        self.queue = Queue.Queue()
        self.patterns = actions.STANDARD_PATTERNS
        self._interpreter = None
        self._feeder = None
        self.modem = modem

    def _stop_interpreter(self):
        """Stop the interpreter."""
        self._interpreter.active = False
        self._interpreter.queue.put('')

    def _start_interpreter(self):
        """Instanciate and start a new interpreter."""
        self._interpreter = Interpreter(self.queue, self.modem, self.patterns)
        self._interpreter.start()

    def start(self):
        """Start the prober.

        Starts two threads, an instance of QueueFeeder and Interpreter.
        """
        if self._feeder:
            raise errors.HumodUsageError('Prober already started.')
        else:
            self._feeder = QueueFeeder(self.queue, self.modem.ctrl_port, 
                                       self.modem.ctrl_lock)
            self._feeder.start()
            self._start_interpreter()

    def stop(self):
        """Stop the prober."""
        if self._feeder:
            self._stop_interpreter()
            self._feeder.stop()
            self._feeder = None
        else:
            raise errors.HumodUsageError('Prober not started.')


class ModemPort(serial.Serial):
    """Class extending serial.Serial by humod specific methods."""

    def send(self, text, wait=True, at_cmd=None):
        """Send serial text to the modem.

        Arguments:
            self -- serial port to send to,
            text -- text value to send,
            wait -- wait for and return the output,
            at_cmd -- interpret as an AT command.

        Returns:
            List of strings if wait is set to True.
        """
        if at_cmd:
            self.write('AT%s\r' % text)
        else:
            self.write(text)
        # Read in the echoed text.
        # Check for errors and raise exception with specific error code.
        input_line = self.readline()
        errors.check_for_errors(input_line)
        # Return the result.
        if wait:
            # If the text being sent is an AT command, only relevant context
            # answer (starting with '+command:' value) will be returned by 
            #return_data(). Otherwise any string will be returned.
            return self.return_data(at_cmd)

    def return_data(self, command=None):
        """Read until exit status is returned.

        Returns:
            data: List of right-stripped strings containing output
            of the command.

        Raises:
            AtCommandError: If an error is returned by the modem.
        """
        data = list()
        while 1:
            # Read in one line of input.
            input_line = self.readline().rstrip()
            # Check for errors and raise exception with specific error code.
            errors.check_for_errors(input_line)
            if input_line == 'OK':
                return data
            # Append only related data (starting with "command" contents).
            if command:
                if input_line.startswith(command):
                    prefix_length = len(command)+2
                    data.append(input_line[prefix_length:])
            else:
                # Append only non-empty data.
                if input_line:
                    data.append(input_line)


class ConnectionStatus(object):
    """Data structure representing current state of the modem."""

    def __init__(self):
        self.x = 0
        self.y = 0
        self.rssi = 0
        self.uplink = 0
        self.downlink = 0
        self.bytes_tx = 0
        self.bytes_rx = 0
        self.link_uptime = 0
        self.mode = None
 
    def report(self):
        """Print a report about the current connection status."""
        format = '%20s : %5s'
        mapping = (('Signal Strength', self.rssi),
                   ('X', self.x),
                   ('Y', self.y),
                   ('Bytes rx', self.bytes_rx),
                   ('Bytes tx', self.bytes_tx),
                   ('Uplink (B/s)', self.uplink),
                   ('Downlink (B/s)', self.downlink),
                   ('Seconds uptime', self.link_uptime),
                   ('Mode', self.mode))
        print
        for item in mapping:
            print format % item


class Modem(at_commands.CommandSet):
    """Huawei Modem."""

    status = ConnectionStatus()
    baudrate = '7200000'
    settings = ['modem', 'crtscts', 'defaultroute', 'usehostname', '-detach',
               'noipdefault', 'call', 'humod', 'user', 'ppp', 'usepeerdns',
               'idle', '0', 'logfd', '8']
    _pppd_pid = None
    _dial_num = '*99#'

    def __init__(self, data_port_str=DEFAULT_DATA_PORT, 
                 ctrl_port_str=DEFAULT_CONTROL_PORT):
        """Open a serial connection to the modem."""
        self.data_port = ModemPort()
        self.data_port.setPort(data_port_str)
        self.ctrl_port = ModemPort(ctrl_port_str, 9600, timeout=PROBER_TIMEOUT)
        self.ctrl_lock = threading.Lock()
        self.prober = Prober(self)

    def connect(self):
        """Use pppd to connect to the network."""
        # Modem is not connected if _pppd_pid is set to None.
        if not self._pppd_pid:
            data_port = self.data_port
            data_port.open()
            data_port.send('ATZ\r')
            data_port.send('ATDT%s\r' % self._dial_num, wait=False)
            status = data_port.readline()
            if status.startswith('CONNECT'):
                pppd_args = [PPPD_PATH, self.baudrate, self.data_port.port]\
                             +self.settings
                pid = os.fork()
                if pid:
                    self._pppd_pid = pid
                else:
                    try:
                        os.execv(PPPD_PATH, pppd_args)
                    except:
                        raise errors.PppdError('An error while starting pppd.')
        else:
            raise errors.HumodUsageError('Modem is already connected.')

    def disconnect(self):
        """Disconnect the modem."""
        if self._pppd_pid:
            os.kill(self._pppd_pid, 15)
            self._pppd_pid = None
        else:
            raise errors.HumodUsageError('Not connected.')