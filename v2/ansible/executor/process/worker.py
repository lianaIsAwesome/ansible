# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import Queue
import multiprocessing
import os
import signal
import sys
import time
import traceback

HAS_ATFORK=True
try:
    from Crypto.Random import atfork
except ImportError:
    HAS_ATFORK=False

from ansible.errors import AnsibleError, AnsibleConnectionFailure
from ansible.executor.task_executor import TaskExecutor
from ansible.executor.task_result import TaskResult
from ansible.playbook.handler import Handler
from ansible.playbook.task import Task

from ansible.utils.debug import debug

__all__ = ['ExecutorProcess']


class WorkerProcess(multiprocessing.Process):
    '''
    The worker thread class, which uses TaskExecutor to run tasks
    read from a job queue and pushes results into a results queue
    for reading later.
    '''

    def __init__(self, tqm, main_q, rslt_q, loader, new_stdin):

        # takes a task queue manager as the sole param:
        self._main_q = main_q
        self._rslt_q = rslt_q
        self._loader = loader

        # dupe stdin, if we have one
        try:
            fileno = sys.stdin.fileno()
        except ValueError:
            fileno = None

        self._new_stdin = new_stdin
        if not new_stdin and fileno is not None:
            try:
                self._new_stdin = os.fdopen(os.dup(fileno))
            except OSError, e:
                # couldn't dupe stdin, most likely because it's
                # not a valid file descriptor, so we just rely on
                # using the one that was passed in
                pass

        if self._new_stdin:
            sys.stdin = self._new_stdin

        super(WorkerProcess, self).__init__()

    def run(self):
        '''
        Called when the process is started, and loops indefinitely
        until an error is encountered (typically an IOerror from the
        queue pipe being disconnected). During the loop, we attempt
        to pull tasks off the job queue and run them, pushing the result
        onto the results queue. We also remove the host from the blocked
        hosts list, to signify that they are ready for their next task.
        '''

        if HAS_ATFORK:
            atfork()

        while True:
            task = None
            try:
                if not self._main_q.empty():
                    debug("there's work to be done!")
                    (host, task, job_vars, connection_info) = self._main_q.get(block=False)
                    debug("got a task/handler to work on: %s" % task)

                    new_connection_info = connection_info.set_task_override(task)

                    # execute the task and build a TaskResult from the result
                    debug("running TaskExecutor() for %s/%s" % (host, task))
                    executor_result = TaskExecutor(host, task, job_vars, new_connection_info, self._loader).run()
                    debug("done running TaskExecutor() for %s/%s" % (host, task))
                    task_result = TaskResult(host, task, executor_result)

                    # put the result on the result queue
                    debug("sending task result")
                    self._rslt_q.put(task_result, block=False)
                    debug("done sending task result")

                else:
                    time.sleep(0.1)

            except Queue.Empty:
                pass
            except (IOError, EOFError, KeyboardInterrupt):
                break
            except AnsibleConnectionFailure:
                try:
                    if task:
                        task_result = TaskResult(host, task, dict(unreachable=True))
                        self._rslt_q.put(task_result, block=False)
                except:
                    # FIXME: most likely an abort, catch those kinds of errors specifically
                    break
            except Exception, e:
                debug("WORKER EXCEPTION: %s" % e)
                debug("WORKER EXCEPTION: %s" % traceback.format_exc())
                try:
                    if task:
                        task_result = TaskResult(host, task, dict(failed=True, exception=traceback.format_exc(), stdout=''))
                        self._rslt_q.put(task_result, block=False)
                except:
                    # FIXME: most likely an abort, catch those kinds of errors specifically
                    break

        debug("WORKER PROCESS EXITING")


