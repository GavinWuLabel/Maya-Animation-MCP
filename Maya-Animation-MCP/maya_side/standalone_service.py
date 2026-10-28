"""Dedicated mayapy service. Never import/run main() inside an existing GUI."""
from __future__ import print_function
import argparse
import json
import os
import queue
import sys
import threading
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MainThreadQueue(object):
    """Only the constructing main thread can evaluate queued Maya callbacks."""
    def __init__(self):
        self.owner = threading.get_ident()
        self.pending = queue.Queue()
        self.closed = False
        self.lock = threading.Lock()
    def submit(self, callback):
        with self.lock:
            if self.closed:
                raise RuntimeError("Standalone scheduler is stopped")
            self.pending.put(callback)
    def pump_once(self, timeout=0.05):
        if threading.get_ident() != self.owner:
            raise RuntimeError("Maya queue must be pumped on its owner/main thread")
        try:
            callback = self.pending.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            callback()
        finally:
            self.pending.task_done()
        return True
    def close(self):
        with self.lock:
            self.closed = True


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description="Isolated background Maya bridge")
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--allow-python', action='store_true')
    parser.add_argument('--output-root', default=os.environ.get('MAYA_MCP_OUTPUT_ROOT', os.path.join(PROJECT, 'output')))
    parser.add_argument('--work-root', default=os.path.join(PROJECT, 'runtime', 'background'))
    parser.add_argument('--connection-file', default=os.path.join(PROJECT, 'runtime', 'background_connection.json'))
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    for name in ('output_root', 'work_root', 'connection_file'):
        setattr(args, name, os.path.abspath(os.path.expanduser(getattr(args, name))))
    return args


def main(argv=None):
    args = parse_options(argv)
    output_root, work_root, connection = args.output_root, args.work_root, args.connection_file
    if os.path.exists(connection):
        raise RuntimeError('Background connection metadata already exists; verify/stop its owner before starting another service')
    os.makedirs(output_root, exist_ok=True)
    os.makedirs(os.path.dirname(connection), exist_ok=True)
    os.makedirs(work_root, exist_ok=True)
    stop_file = os.path.join(work_root, 'stop.request')
    if os.path.exists(stop_file):
        os.remove(stop_file)
    # Dedicated profile avoids changing GUI preferences or its startup files.
    os.environ['MAYA_APP_DIR'] = os.path.join(work_root, 'maya_profile')
    sys.path.insert(0, PROJECT)
    import maya.standalone
    initialized = False
    started = False
    scheduler = MainThreadQueue()
    from maya_side import maya_bridge
    try:
        maya.standalone.initialize(name='python')
        initialized = True
        import maya.cmds as cmds
        if not cmds.about(batch=True):
            raise RuntimeError('This entry point requires isolated mayapy batch mode')
        info = maya_bridge.start(connection_file=connection, port=args.port,
                                 output_root=output_root, allow_python=args.allow_python,
                                 defer=scheduler.submit)
        started = True
        # Never print/persist the token outside the private connection file.
        ready = {'pid':os.getpid(), 'port':info['port'], 'protocol':info['protocol'],
                 'connection_file':connection, 'output_root':output_root,
                 'batch':True, 'scheduler':'main_thread_queue',
                 'allow_python':args.allow_python, 'started_unix':time.time()}
        with open(os.path.join(work_root,'ready.json'),'w',encoding='utf8') as stream:
            json.dump(ready,stream,ensure_ascii=False,indent=2)
        print('BACKGROUND_MAYA_READY pid=%s port=%s scheduler=main_thread_queue' % (os.getpid(),info['port']))
        sys.stdout.flush()
        while not os.path.exists(stop_file):
            scheduler.pump_once()
    finally:
        if started:
            maya_bridge.stop()
            # Drain invalidated jobs so they return SESSION_EXPIRED; no Maya mutation.
            while scheduler.pump_once(timeout=0):
                pass
        scheduler.close()
        if initialized:
            maya.standalone.uninitialize()

if __name__ == '__main__':
    main()
