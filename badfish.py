#! /usr/bin/env python

from core.logger import Logger
from logging import FileHandler, Formatter

import argparse
import json
import os
import re
import requests
import sys
import time
import warnings
import yaml

warnings.filterwarnings("ignore")


BOOT_SOURCES_URI = "/redfish/v1/Systems/System.Embedded.1/BootSources/Settings"
BIOS_URI = "/redfish/v1/Systems/System.Embedded.1/Bios/Settings"


class Badfish:
    def __init__(self, _host, _username, _password):
        self.host = _host
        self.username = _username
        self.password = _password
        self.logger = Logger()

    def get_bios_boot_mode(self):
        _response = requests.get(
            "https://%s/redfish/v1/Systems/System.Embedded.1/Bios" % self.host,
            verify=False,
            auth=(self.username, self.password),
        )
        try:
            data = _response.json()
        except ValueError:
            self.logger.error("Could not retrieve Bios Boot mode.")
            sys.exit(1)
        return data[u"Attributes"]["BootMode"]

    @staticmethod
    def get_boot_seq(bios_boot_mode):
        if bios_boot_mode == "Uefi":
            return "UefiBootSeq"
        else:
            return "BootSeq"

    def get_boot_devices(self, _boot_seq):
        _response = requests.get(
            "https://%s/redfish/v1/Systems/System.Embedded.1/BootSources" % self.host,
            verify=False,
            auth=(self.username, self.password),
        )
        data = _response.json()
        return data[u"Attributes"][_boot_seq]

    def change_boot_order(self, _interfaces_path, _host_type):
        with open(_interfaces_path, "r") as f:
            try:
                definitions = yaml.safe_load(f)
            except yaml.YAMLError as ex:
                self.logger.error(ex)
                sys.exit(1)

        host_model = self.host.split(".")[0].split("-")[-1]
        interfaces = definitions["%s_%s_interfaces" % (_host_type, host_model)].split(",")

        bios_boot_mode = self.get_bios_boot_mode()
        boot_seq = self.get_boot_seq(bios_boot_mode)
        boot_devices = self.get_boot_devices(boot_seq)
        change = False
        for i in range(len(interfaces)):
            for device in boot_devices:
                if interfaces[i] == device[u"Name"]:
                    if device[u"Index"] != i:
                        device[u"Index"] = i
                        change = True
                    break

        if change:
            url = "https://%s%s" % (self.host, BOOT_SOURCES_URI)
            payload = {"Attributes": {boot_seq: boot_devices}}
            headers = {"content-type": "application/json"}
            response = requests.patch(
                url, data=json.dumps(payload), headers=headers, verify=False, auth=(self.username, self.password)
            )
            if response.status_code == 200:
                self.logger.info("- PASS: PATCH command passed to update boot order")
            else:
                self.logger.error("- FAIL: There was something wrong with your request")
        else:
            self.logger.warning("- WARNING: No changes were made since the boot order already matches the requested.")
            sys.exit()
        return

    def set_next_boot_pxe(self):
        _url = "https://%s/redfish/v1/Systems/System.Embedded.1" % self.host
        _payload = {"Boot": {"BootSourceOverrideTarget": "Pxe"}}
        _headers = {"content-type": "application/json"}
        _response = requests.patch(
            _url, data=json.dumps(_payload), headers=_headers, verify=False, auth=(self.username, self.password)
        )
        time.sleep(5)
        if _response.status_code == 200:
            self.logger.info('- PASS: PATCH command passed to set next boot onetime boot device to: "%s"' % "Pxe")
        else:
            self.logger.error("- FAIL: Command failed, error code is %s" % _response.status_code)
            detail_message = str(_response.__dict__)
            self.logger.error(detail_message)
            sys.exit(1)
        return

    def clear_job_queue(self, _job_queue):
        _url = "https://%s/redfish/v1/Managers/iDRAC.Embedded.1/Jobs" % self.host
        _headers = {"content-type": "application/json"}
        if not _job_queue:
            self.logger.warning(
                "\n- WARNING, job queue already cleared for iDRAC %s, DELETE command will not execute" % self.host
            )
            return
        self.logger.warning("\n- WARNING, clearing job queue for job IDs: %s\n" % _job_queue)
        for _job in _job_queue:
            job = _job.strip("'")
            url = "%s/%s" % (_url, job)
            requests.delete(url, headers=_headers, verify=False, auth=(self.username, self.password))
        job_queue = self.get_job_queue()
        if not job_queue:
            self.logger.info("- PASS, job queue for iDRAC %s successfully cleared" % self.host)
        else:
            self.logger.error("- FAIL, job queue not cleared, current job queue contains jobs: %s" % job_queue)
            sys.exit(1)
        return

    def get_job_queue(self):
        _url = "https://%s/redfish/v1/Managers/iDRAC.Embedded.1/Jobs" % self.host
        _response = requests.get(_url, auth=(self.username, self.password), verify=False)
        data = _response.json()
        data = str(data)
        job_queue = re.findall("JID_.+?'", data)
        return job_queue

    def create_bios_config_job(self, uri):
        _url = "https://%s/redfish/v1/Managers/iDRAC.Embedded.1/Jobs" % self.host
        _payload = {"TargetSettingsURI": uri}
        _headers = {"content-type": "application/json"}
        _response = requests.post(
            _url, data=json.dumps(_payload), headers=_headers, verify=False, auth=(self.username, self.password)
        )
        status_code = _response.status_code

        if status_code == 200:
            self.logger.info("- PASS: POST command passed to create target config job, status code 200 returned.")
        else:
            self.logger.error("- FAIL: POST command failed to create BIOS config job, status code is %s" % status_code)
            detail_message = str(_response.__dict__)
            self.logger.error(detail_message)
            sys.exit(1)
        convert_to_string = str(_response.__dict__)
        job_id_search = re.search("JID_.+?,", convert_to_string).group()
        _job_id = re.sub("[,']", "", job_id_search)
        self.logger.info("- INFO: %s job ID successfully created" % _job_id)
        return _job_id

    def get_job_status(self, _job_id):
        retries = 10
        for _ in range(retries):
            req = requests.get(
                "https://%s/redfish/v1/Managers/iDRAC.Embedded.1/Jobs/%s" % (self.host, _job_id),
                verify=False,
                auth=(self.username, self.password),
            )
            status_code = req.status_code
            if status_code == 200:
                self.logger.info("- PASS: Command passed to check job status, code 200 returned")
                time.sleep(10)
            else:
                self.logger.error("- FAIL: Command failed to check job status, return code is %s" % status_code)
                self.logger.error("    Extended Info Message: {0}".format(req.json()))
                sys.exit(1)
            data = req.json()
            if data[u"Message"] == "Task successfully scheduled.":
                self.logger.info("- PASS: job id %s successfully scheduled" % _job_id)
                return
            else:
                self.logger.warning("- WARNING: JobStatus not scheduled, current status is: %s" % data[u"Message"])
        self.logger.error("- FAIL: Not able to successfully schedule the job.")
        sys.exit(1)

    def reboot_server(self):
        _response = requests.get(
            "https://%s/redfish/v1/Systems/System.Embedded.1/" % self.host,
            verify=False,
            auth=(self.username, self.password),
        )
        data = _response.json()
        self.logger.warning("- WARNING: Current server power state is: %s" % data[u"PowerState"])
        if data[u"PowerState"] == "On":
            _url = "https://%s/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset" % self.host
            _payload = {"ResetType": "GracefulRestart"}
            _headers = {"content-type": "application/json"}
            _response = requests.post(
                _url, data=json.dumps(_payload), headers=_headers, verify=False, auth=(self.username, self.password)
            )
            status_code = _response.status_code
            if status_code == 204:
                self.logger.info(
                    "- PASS: Command passed to gracefully restart server, code return is %s" % status_code
                )
                time.sleep(3)
            else:
                self.logger.error(
                    "- FAIL: Command failed to gracefully restart server, status code is: %s" % status_code
                )
                self.logger.error("    Extended Info Message: {0}".format(_response.json()))
                sys.exit(1)
            count = 0
            while True:
                _response = requests.get(
                    "https://%s/redfish/v1/Systems/System.Embedded.1/" % self.host,
                    verify=False,
                    auth=(self.username, self.password),
                )
                data = _response.json()
                if data[u"PowerState"] == "Off":
                    self.logger.info("- PASS: GET command passed to verify server is in OFF state")
                    break
                elif count == 10:
                    self.logger.warning(
                        "- WARNING: unable to graceful shutdown the server, will perform forced shutdown now"
                    )
                    _url = "https://%s/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset" % self.host
                    _payload = {"ResetType": "ForceOff"}
                    _headers = {"content-type": "application/json"}
                    _response = requests.post(
                        _url,
                        data=json.dumps(_payload),
                        headers=_headers,
                        verify=False,
                        auth=(self.username, self.password),
                    )
                    status_code = _response.status_code
                    if status_code == 204:
                        self.logger.info(
                            "- PASS: Command passed to forcefully power OFF server, code return is %s" % status_code
                        )
                        time.sleep(10)
                    else:
                        self.logger.error(
                            "- FAIL, Command failed to gracefully power OFF server, status code is: %s" % status_code
                        )
                        self.logger.error("    Extended Info Message: {0}".format(_response.json()))
                        sys.exit(1)

                else:
                    time.sleep(1)
                    count += 1
                    continue

            _payload = {"ResetType": "On"}
            _headers = {"content-type": "application/json"}
            _response = requests.post(
                _url, data=json.dumps(_payload), headers=_headers, verify=False, auth=(self.username, self.password)
            )
            status_code = _response.status_code
            if status_code == 204:
                self.logger.info("- PASS: Command passed to power ON server, code return is %s" % status_code)
            else:
                self.logger.error("- FAIL: Command failed to power ON server, status code is: %s" % status_code)
                self.logger.error("    Extended Info Message: {0}".format(_response.json()))
                sys.exit(1)
        elif data[u"PowerState"] == "Off":
            _url = "https://%s/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset" % self.host
            _payload = {"ResetType": "On"}
            _headers = {"content-type": "application/json"}
            _response = requests.post(
                _url, data=json.dumps(_payload), headers=_headers, verify=False, auth=(self.username, self.password)
            )
            status_code = _response.status_code
            if status_code == 204:
                self.logger.info("- PASS: Command passed to power ON server, code return is %s" % status_code)
            else:
                self.logger.error("- FAIL: Command failed to power ON server, status code is: %s" % status_code)
                self.logger.error("    Extended Info Message: {0}".format(_response.json()))
                sys.exit(1)
        else:
            self.logger.error("- FAIL: unable to get current server power state to perform either reboot or power on")
            sys.exit(1)

    def boot_to_device(self, _boot_to):
        _url = "https://%s%s" % (self.host, BIOS_URI)
        _payload = {"Attributes": {"OneTimeBootMode": "OneTimeBootSeq", "OneTimeBootSeqDev": _boot_to}}
        _headers = {"content-type": "application/json"}
        _response = requests.patch(
            _url, data=json.dumps(_payload), headers=_headers, verify=False, auth=(self.username, self.password)
        )
        status_code = _response.status_code
        if status_code == 200:
            self.logger.info("- PASS: Command passed to set BIOS attribute pending values")
        else:
            self.logger.error("- FAIL: command failed, error code is: %s" % status_code)
            self.logger.error(str(_response.__dict__))
            sys.exit(1)

    def check_boot(self, _interfaces_path):
        bios_boot_mode = self.get_bios_boot_mode()
        boot_seq = self.get_boot_seq(bios_boot_mode)
        boot_devices = self.get_boot_devices(boot_seq)
        if _interfaces_path:

            with open(_interfaces_path, "r") as f:
                try:
                    definitions = yaml.safe_load(f)
                except yaml.YAMLError as ex:
                    self.logger.error(ex)
                    return 1

            host_model = self.host.split(".")[0].split("-")[-1]
            interfaces = {}
            for _host in ["foreman", "director"]:
                match = True
                interfaces[_host] = definitions["%s_%s_interfaces" % (_host, host_model)].split(",")
                for device in sorted(boot_devices[: len(interfaces)], key=lambda x: x[u"Index"]):
                    if device[u"Name"] == interfaces[_host][device[u"Index"]]:
                        continue
                    else:
                        match = False
                        break
                if match:
                    self.logger.info("Current boot order is set to '%s'" % _host)
                    return

            self.logger.warning("- WARN: Current boot order does not match any of the given.")
            self.logger.info("Current boot order:")
            for device in sorted(boot_devices, key=lambda x: x[u"Index"]):
                self.logger.info("%s: %s" % (int(device[u"Index"]) + 1, device[u"Name"]))
            return

        else:
            self.logger.info("Current boot order:")
            for device in sorted(boot_devices, key=lambda x: x[u"Index"]):
                self.logger.info("%s: %s" % (int(device[u"Index"]) + 1, device[u"Name"]))
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description="Client tool for changing boot order via Redfish API.")
    parser.add_argument("-H", help="iDRAC host address", required=True)
    parser.add_argument("-u", help="iDRAC username", required=True)
    parser.add_argument("-p", help="iDRAC password", required=True)
    parser.add_argument("-i", help="Path to iDRAC interfaces yaml", default=None)
    parser.add_argument("-t", help="Type of host. Accepts: foreman, director")
    parser.add_argument("-l", "--log", help="Optional argument for logging results to a file")
    parser.add_argument("--pxe", help="Set next boot to one-shot boot PXE", action="store_true")
    parser.add_argument("--boot-to", help="Set next boot to one-shot boot to a specific device")
    parser.add_argument("--reboot-only", help="Flag for only rebooting the host", action="store_true")
    parser.add_argument("--check-boot", help="Flag for checking the host boot order", action="store_true")
    args = vars(parser.parse_args(argv))
    host = args["H"]
    username = args["u"]
    password = args["p"]
    host_type = args["t"]
    log = args["log"]
    interfaces_path = args["i"]
    pxe = args["pxe"]
    boot_to = args["boot_to"]
    reboot_only = args["reboot_only"]
    check_boot = args["check_boot"]
    badfish = Badfish(host, username, password)
    badfish.logger.start()

    if log:
        file_handler = FileHandler(log)
        file_handler.setFormatter(Formatter(badfish.logger.LOGFMT))
        file_handler.setLevel("DEBUG")
        badfish.logger.addHandler(file_handler)

    if reboot_only:
        badfish.reboot_server()
    elif boot_to:
        badfish.boot_to_device(boot_to)
        jobs_queue = badfish.get_job_queue()
        if jobs_queue:
            badfish.clear_job_queue(jobs_queue)
        job_id = badfish.create_bios_config_job(BIOS_URI)
        badfish.get_job_status(job_id)
        badfish.reboot_server()
    elif check_boot:
        badfish.check_boot(interfaces_path)
    else:
        if host_type:
            if host_type.lower() not in ("foreman", "director"):
                raise argparse.ArgumentTypeError('Expected values for -t argument are "foreman" or "director"')

        if interfaces_path:
            if not os.path.exists(interfaces_path):
                badfish.logger.error("- FAIL: No such file or directory: %s" % interfaces_path)
                return 1
            badfish.change_boot_order(interfaces_path, host_type)
        else:
            badfish.logger.error("- FAIL: You must provide a path to the interfaces yaml via `-i` optional argument.")
            return 1
        if pxe:
            badfish.set_next_boot_pxe()
        jobs_queue = badfish.get_job_queue()
        if jobs_queue:
            badfish.clear_job_queue(jobs_queue)
        job_id = badfish.create_bios_config_job(BIOS_URI)
        badfish.get_job_status(job_id)
        badfish.reboot_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())