#!/usr/bin/env python
# Copyright (C) 2010-2014 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import argparse
import logging
import os.path
import sys

sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))

from lib.cuckoo.core.database import Database, TASK_RECURRENT
from lib.cuckoo.common.utils import time_duration

MACHINE_CRONTAB = """
#!/bin/sh
# Copyright (C) 2010-2014 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

# This script should be run under the "cuckoo" user (assuming that's the
# user under which Cuckoo Sandbox runs).
set -e

# Cronjobs generally don't have much in $PATH.
export PATH="$PATH:/usr/bin:/usr/local/bin"

# Provides an exclusive lock around the following code (which should never be
# executed in parallel). Upon failure to obtain the lock it'll simply exit.
(flock -nx 9 || exit 0

CUCKOO="%(cuckoo)s"
EXPERIMENT="$CUCKOO/utils/experiment.py"
BASEDIR="%(basedir)s"
VMS="$BASEDIR/vms"

# We strive to have at least 20 provisioned virtual machines at any point.
while [ "$("$EXPERIMENT" count-available-machines verbose=false)" -lt 20 ]; do
    IPADDR="$("$EXPERIMENT" allocate-ipaddr verbose=false)"
    EGGNAME="$("$EXPERIMENT" allocate-eggname verbose=false)"
    RDPPORT="$("$EXPERIMENT" allocate-rdp-port verbose=false)"
    vmcloak-clone -r --bird winxp_bird --hostonly-ip "$IPADDR" \\
        --vmmode longterm --cuckoo "$CUCKOO" --cpu-count %(cpucount)s \\
        --vrde --vrde-port "$RDPPORT" --vm-dir "$VMS" \\
        --data-dir "$VMS" "$EGGNAME"
done

) 9>/var/lock/longtermvmprovision
"""

def allocate_ip_address():
    """Allocate the next available IP address."""
    # TODO Unuglify this code.
    ips = []
    for machine in db.list_machines():
        ipa, ipb, ipc, ipd = machine.ip.split(".")
        ips.append([int(ipa), int(ipb), int(ipc), int(ipd)])

    if ips:
        max_ip = sorted(ips, reverse=True)[0]
    else:
        max_ip = [192, 168, 56, 2]

    # Calculate the next IP address. When the lowest 8 bits of the IP address
    # have reached .254, we iterate to the next /24 block. It is therefore
    # assumed that upcoming /24 blocks are also within the same vboxnet, or
    # otherwise the iptables routing will not work. (I.e., when creating new
    # clones the new IP address may not lay in the same /24 and therefore IP
    # routing is no longer functional).
    # 192.168.56.42  -> 192.168.56.43
    # 192.168.56.254 -> 192.168.57.1
    if max_ip[3] == 254:
        max_ip[2] += 1
        max_ip[3] = 1
    else:
        max_ip[3] += 1

    return "%d.%d.%d.%d" % tuple(max_ip)

def allocate_eggname():
    """Allocate the next available eggname."""
    eggs = []
    for machine in db.list_machines():
        assert machine.name.startswith("winxp_")
        eggs.append(int(machine.name[6:]))

    if eggs:
        max_egg = sorted(eggs, reverse=True)[0]
    else:
        max_egg = 0

    # Calculate the next eggname.
    return "winxp_%04d" % (max_egg + 1)

def allocate_rdp_port():
    """Allocate the next available RDP port.

    Uses the eggname, extracts the integer, and prepends a one. As result
    winxp_0001 will receive RDP port number 10001.
    """
    eggname = allocate_eggname()
    assert eggname.startswith("winxp_")
    return "1%s" % eggname[6:]

class ExperimentManager(object):
    ARGUMENTS = {
        "help": "action",
        "list": "",
        "new": "name path | timeout delta tags options",
        "schedule": "name | delta timeout",
        "count_available_machines": "| verbose",
        "machine_cronjob": "",
        "allocate_ipaddr": "| verbose",
        "allocate_eggname": "| verbose",
        "allocate_rdp_port": "| verbose",
        "delta": "name | delta",
        "timeout": "name | timeout",
    }

    def check_arguments(self, action, args, kwargs):
        opt = False
        for idx, arg in enumerate(self.ARGUMENTS[action].split()):
            if arg == "|":
                opt = True
                continue

            if not opt and idx >= len(args) and arg not in kwargs:
                return arg

    def handle_help(self, action):
        """Show help on an action.

        action  = Action to get help on.

        """
        action = action.replace("-", "_")
        if not hasattr(self, "handle_%s" % action):
            print "Unknown action:", action
            exit(1)

        first, doc = True, getattr(self, "handle_%s" % action).__doc__
        for line in doc.strip().split("\n"):
            if not line.strip():
                first = False

            if line.strip().startswith("[") and line.strip().endswith("]"):
                print "Opt.  ", line.strip()[1:-1]
            elif not first:
                print "      ", line.strip()
            else:
                print line.strip()

    def handle_list(self):
        """List all available experiments."""
        print "%20s | %16s" % ("Name", "Experiment Count")
        fmt = "%(name)20s | %(count)16d"
        for experiment in db.list_experiments():
            print fmt % dict(name=experiment.name, count=len(experiment.tasks))

    def handle_new(self, name, path, timeout="1d", delta="1d", tags="",
                   options=""):
        """Create a new experiment.

        name    = Experiment name.
        path    = File path.
        [timeout = Duration of the analysis.]
        [delta   = Relative time between the last and the next task.]
        [tags    = Extra tags.]
        [options = Extra options.]

        """
        # TODO Add more options which don't seem too relevant at the moment.
        task_id = db.add_path(file_path=path,
                              timeout=time_duration(timeout),
                              tags="longterm," + tags,
                              options=options,
                              name=name,
                              repeat=TASK_RECURRENT,
                              delta=delta)

        print "Created experiment '%s' with ID: %d" % (name, task_id)

    def handle_schedule(self, name, delta="1d", timeout="1d"):
        """Schedule the next time and date for an experiment.

        name    = Experiment name.
        [delta   = Relative time after the last task.]
        [timeout = Duration of the analysis.]

        """
        experiment = db.view_experiment(name=name)
        last_task = experiment.tasks.order_by("id desc").first()
        if not last_task:
            print "Tasks with experiment name '%s' not found.." % name
            exit(1)

        task = db.schedule(last_task.id, delta=time_duration(delta),
                           timeout=time_duration(timeout))
        print "Scheduled experiment '%s' with ID: %d" % (name, task.id)

    def handle_delta(self, name, delta=None):
        """Get or set the delta between multiple analyses for the upcoming
        analysis of an experiment.

        name    = Experiment name.
        [delta   = Updated relative time after the last task.]

        """
        if delta is not None:
            db.update_experiment(name, delta=delta)

    def handle_timeout(self, name, timeout=None):
        """Get or set the duration of an analysis for the upcoming analysis
        of an experiment.

        name    = Experiment name.
        [timeout = Updated duration of the analysis.]

        """
        if timeout is not None:
            db.update_experiment(name, timeout=timeout)

    def handle_count_available_machines(self, verbose=True):
        """Count the available machines for longterm analysis.

        [verbose = Verbose output.]

        """
        # TODO Allow tags to be specified.
        if verbose:
            print "Available machines:", db.count_machines_available()
        else:
            print db.count_machines_available()

    def handle_machine_cronjob(self, action="dump", cpucount=1, path=None,
                               basedir="/home/cuckoo"):
        """Manage the machine cronjob - for provisioning virtual machines
        for longterm analysis.

        [action   = Action to perform.]
        [path     = Cronjob path in install mode.]
        [cpucount = CPU Count for the Virtual Machines.]
        [basedir  = Base directory for Virtual Machines.]

        """
        cuckoo = os.path.abspath(os.path.join(__file__, "..", ".."))

        args = dict(cuckoo=cuckoo, cpucount=cpucount, basedir=basedir)
        cronjob = MACHINE_CRONTAB.strip() % args

        if action == "dump":
            print cronjob
        elif action == "install":
            open(path, "wb").write(cronjob)

    def handle_allocate_ipaddr(self, verbose=True):
        """Calculate the next available IP address. If a new network interface
        is required to allocate a new IP address, then this is also handled.
        Doesn't actually allocate a new IP address but merely calculates it.

        [verbose = Verbose output.]

        """
        ip = allocate_ip_address()
        if not ip:
            exit(1)

        if verbose:
            print "Next IP address:", ip
        else:
            print ip

    def handle_allocate_eggname(self, verbose=True):
        """Calculate the next available egg name.

        [verbose = Verbose output.]

        """
        eggname = allocate_eggname()
        if not eggname:
            exit(1)

        if verbose:
            print "Next egg name:", eggname
        else:
            print eggname

    def handle_allocate_rdp_port(self, verbose=True):
        """Calculate the next available RDP port based on the last egg name.

        [verbose = Verbose output.]

        """
        rdp_port = allocate_rdp_port()
        if not rdp_port:
            exit(1)

        if verbose:
            print "Next RDP port:", rdp_port
        else:
            print rdp_port

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", type=str, help="Action to perform")
    parser.add_argument("arguments", type=str, nargs="*", help="Arguments for the action")
    args = parser.parse_args()

    action = args.action.replace("-", "_")

    em = ExperimentManager()
    if not hasattr(em, "handle_%s" % action):
        print "Invalid action:", action
        exit(1)

    values = {
        "true": True,
        "false": False,
    }

    args_, kwargs = [], {}
    for arg in args.arguments:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kwargs[k.strip()] = values.get(v.strip(), v.strip())
        else:
            args_.append(arg.strip())

    ret = em.check_arguments(action, args_, kwargs)
    if ret:
        print "Missing argument:", ret
        print
        em.handle_help(action)
        exit(1)

    getattr(em, "handle_%s" % action)(*args_, **kwargs)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = Database()
    main()
