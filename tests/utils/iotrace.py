#
# Copyright(c) 2019 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#

import json
import re
import time
from datetime import timedelta

from api.iotrace_lat_hist_parser import HistogramEntry, LatencyHistogram
from core.test_run_utils import TestRun
from test_utils.singleton import Singleton
from utils.installer import check_if_installed
from test_utils.size import Size, Unit

NOT_WHITESPACE = re.compile(r'[^\s]')


# Singleton class to provide test-session wide scope
class IotracePlugin(metaclass=Singleton):
    def __init__(self, repo_dir, working_dir):
        self.repo_dir = repo_dir  # Test controller's repo, copied to DUT
        self.working_dir = working_dir  # DUT's make/install work directory
        self.installed = check_if_installed()  # Was iotrace installed already

    def start_tracing(self, bdevs=[], buf=None):
        '''
        Start tracing given block devices. Trace all available if none given.

        :param dev_list: Block devices to trace, can be empty (for all available)
        :type dev_list: list of strings
        :param buf: Size of tracing buffer
        :type buf: Size

        :raises Exception: if iotrace binary exited early
        '''

        if len(bdevs) == 0:
            disks = TestRun.dut.disks
            for disk in disks:
                bdevs.append(disk.system_path)

        opts = ""
        if buf:
            buf = int(buf.get_value(Unit.MiB))
            opts += f" -b {buf}"

        TestRun.executor.run_in_background('iotrace -S -d ' + ','.join(bdevs) + opts)
        TestRun.LOGGER.info("Started tracing of: " + ','.join(bdevs))

    def get_trace_repository_path(self) -> str:
        '''
        Get the path to trace repository from iotrace

        :return: JSON string with trace repository path
        :raises Exception: when cannot find the path
        '''
        return TestRun.executor.run_expect_success(
            'iotrace --trace-config --get-trace-repository-path').stdout

    def check_if_tracing_active(self) -> bool:
        '''
        Check if tracing is active

        :return: True if iotrace process found, False otherwise
        '''
        output = TestRun.executor.run('pgrep iotrace')
        if output.stdout == "":
            return False
        else:
            return True

    def stop_tracing(self) -> bool:
        '''
        Stop tracing.

        :return: True if tracing was stopped, False no tracing was active
        :raises Exception: if could not stop tracing which is active
        '''
        TestRun.LOGGER.info("Stopping tracing")
        pid = TestRun.executor.run('pgrep iotrace')
        if pid.exit_code != 0:
            return False

        # Send sigints
        kill_attempts = 30
        attempt = 0
        while pid.stdout != "" and attempt < kill_attempts:
            TestRun.LOGGER.info("Sending sigint no. " + str(attempt))
            attempt += 1
            TestRun.executor.run(f'kill -s SIGINT {pid.stdout}')
            time.sleep(2)
            pid = TestRun.executor.run('pgrep iotrace')

        if pid.stdout != "":
            raise Exception("Could not kill iotrace")

        return True

    def get_latest_trace_path(self) -> str:
        '''
        Returns trace path of most recent trace

        :return: trace path
        '''
        output = TestRun.executor.run_expect_success(
            'iotrace --trace-management -L')
        paths_parsed = self.parse_json(output.stdout)

        # Sort trace paths
        def get_sort_key(element):
            return element['tracePath']

        paths_parsed[0]['trace'].sort(key=get_sort_key)

        # Return the last element of trace list
        if len(paths_parsed):
            return paths_parsed[0]['trace'][-1]['tracePath']
        else:
            return ""

    def get_trace_summary(self, trace_path: str) -> str:
        '''
        Get trace summary of given trace path

        :param trace_path: trace path
        :return: Summary of trace in JSON format
        :raises Exception: if summary is invalid
        '''
        output = TestRun.executor.run(
            f'iotrace --trace-management --get-trace-summary -p {trace_path}')
        if (output.stdout == ""):
            raise Exception("Invalid summary")

        return output.stdout

    def get_lba_histogram(self, trace_path: str, bucket_size=0,
                          subrange_start=0, subrange_end=0) -> str:
        '''
        Get lba histogram of given trace path

        :param trace_path: trace path
        :param bucket_size: bucket size
        :param subrange_start: subrange start
        :param subrange_end: subrange end
        :return: LBA histogram in JSON format
        :raises Exception: if histogram is invalid
        '''
        output = TestRun.executor.run(
            f'iotrace --trace-parser --lba-histogram -p {trace_path} -b {bucket_size} -s {subrange_start} -e {subrange_end}')
        if (output.stdout == ""):
            raise Exception("Invalid histogram")

        return output.stdout

    def get_trace_events(self, trace_path: str) -> str:
        '''
        Get all trace events of given trace path

        :param trace_path: trace path
        :return: Trace events in JSON format
        :raises Exception: if traces are invalid
        '''
        output = TestRun.executor.run(
            f'iotrace --trace-parser --io -p {trace_path}')
        if (output.stdout == ""):
            raise Exception("Invalid traces")

        return output.stdout

    def get_fs_statistics(self, trace_path: str) -> list:
        '''
        Get FS statistics for given trace

        :param trace_path: trace_path
        :return: Statistics in JSON format
        :raises Exception: if statistics couldn't be retrieved
        '''
        output = TestRun.executor.run_expect_success(f"iotrace -P -F -p {trace_path}")
        if (output.stdout == ""):
            raise Exception("Couldn't retrieve statistics")

        return self.parse_json(output.stdout)[0]["entries"]

    def get_trace_statistics(self, trace_path: str) -> str:
        '''
        Get statistics of particular trace

        :param trace_path: trace path
        :return: Trace events in JSON format
        :raises Exception: if traces are invalid
        '''
        output = TestRun.executor.run(
            f'iotrace --trace-parser --statistics -p {trace_path}')
        if (output.stdout == ""):
            raise Exception("Invalid traces")

        return self.parse_json(output.stdout)[0]['statistics'][0]

    def get_latency_histogram(self, trace_path: str) -> str:
        '''
        Get latency histogram for particular trace

        :param trace_path: trace path
        :return: Latency histogram in JSON format
        :raises Exception: if traces are invalid
        '''
        out = TestRun.executor.run_expect_success(
                f'iotrace --trace-parser --latency-histogram -p {trace_path}').stdout

        return LatencyHistogram(self.parse_json(out)[0]['histogram'][0])

    def parse_json(self, output: str):
        '''
        Parse a string with json messages to a list of python dictionaries

        :param output: JSON output to be parsed into python dicts

        :return: List of dictionaries with fields corresponding to json messages fields
        :rtype: List of dicts
        '''
        msgs_list = []
        for obj in self.__decode_json_stream(output):
            msgs_list.append(obj)
        return msgs_list

    # Get json objects from a stream of concatenated json messages
    #
    # raw_decode stops once it has a valid object and returns the last position
    # where wasn't part of the parsed object. It's not documented, but you can
    # pass this position back to raw_decode and it start parsing again from that
    # position. Unfortunately, the Python json module doesn't accept strings
    # that have prefixing whitespace. So we need to search to find the first
    # none-whitespace part of your document.
    def __decode_json_stream(self, document, pos=0, decoder=json.JSONDecoder()):
        while True:
            # Create json stream without whitespace
            match = NOT_WHITESPACE.search(document, pos)

            if not match:
                # No more data
                return
            pos = match.start()

            try:
                obj, pos = decoder.raw_decode(document, pos)
            except json.JSONDecodeError:
                raise Exception("Invalid json formatting")

            yield obj
