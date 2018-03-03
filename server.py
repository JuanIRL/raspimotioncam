#!/usr/bin/env python

import sys
import io
import os
import shutil
from subprocess import Popen, PIPE
from string import Template
from struct import Struct
from threading import Thread
from time import sleep, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from wsgiref.simple_server import make_server

import picamera
from ws4py.websocket import WebSocket
from ws4py.server.wsgirefserver import WSGIServer, WebSocketWSGIRequestHandler
from ws4py.server.wsgiutils import WebSocketWSGIApplication

from urllib.parse import urlparse, parse_qs
import configparser
from gpiozero import MotionSensor
from time import sleep, time
from pushetta import Pushetta

###########################################
# CONFIGURATION
WIDTH = 640
HEIGHT = 480
FRAMERATE = 24
HTTP_PORT = 8082
WS_PORT = 8084
COLOR = u'#444'
BGCOLOR = u'#333'
JSMPEG_MAGIC = b'jsmp'
JSMPEG_HEADER = Struct('>4sHH')

config = configparser.ConfigParser()
PUSHETTA_KEY = "e591b03544179db61e747dddccceac0fa3714d26"
CHANNEL_NAME = "SensorRPI"
alarm = Pushetta(PUSHETTA_KEY)
pir = MotionSensor(4)
###########################################


class StreamingHttpHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        config.read('config.ini')
        ACTIVE = config.get('main','active')
        STREAMING = config.get('main', 'streaming')
        SAVE = config.get('main', 'save')
        ACTIVE_CHECK = ''
        SAVE_CHECK=''
        if ACTIVE == 'on':
            ACTIVE_CHECK = 'checked'
        if SAVE == 'on':
            SAVE_CHECK = 'checked'
        url = urlparse("http://localhost:8082" + self.path)
        path = url.path
        query = parse_qs(url.query)
        if path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
            return
        elif path == '/jsmpg.js':
            content_type = 'application/javascript'
            content = self.server.jsmpg_content
        elif path == '/index.html':
            content_type = 'text/html; charset=utf-8'
            tpl = Template(self.server.index_template)
            content = tpl.safe_substitute(dict(
                ADDRESS='%s:%d' % (self.request.getsockname()[0], WS_PORT),
                WIDTH=WIDTH, HEIGHT=HEIGHT, COLOR=COLOR, BGCOLOR=BGCOLOR, ACTIVE=ACTIVE_CHECK, SAVE=SAVE_CHECK))
        elif path == '/config':
            if 'active' in query:
                print("ACTIVE: " + query['active'][0])
                ACTIVE = 'on'
                ACTIVE_CHECK = 'checked'
            else:
                print("ACTIVE: off")
                ACTIVE = 'off'
                ACTIVE_CHECK = ''
            if 'save' in query:
                print("SAVE: " + query['save'][0])
                SAVE = 'on'
                SAVE_CHECK = 'checked'
            else:
                print("SAVE: off")
                SAVE = 'off'
                SAVE_CHECK = ''
            config.set('main', 'active', ACTIVE)
            config.set('main', 'save', SAVE)
            with open('config.ini', 'w') as f:
                config.write(f)
            content_type = 'text/html; charset=utf-8'
            tpl = Template(self.server.index_template)
            content = tpl.safe_substitute(dict(
                ADDRESS='%s:%d' % (self.request.getsockname()[0], WS_PORT),
                WIDTH=WIDTH, HEIGHT=HEIGHT, COLOR=COLOR, BGCOLOR=BGCOLOR, ACTIVE=ACTIVE_CHECK, SAVE=SAVE_CHECK))
        else:
            self.send_error(404, 'File not found')
            return
        content = content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(content))
        self.send_header('Last-Modified', self.date_time_string(time()))
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(content)


class StreamingHttpServer(HTTPServer):
    def __init__(self):
        super(StreamingHttpServer, self).__init__(
                ('', HTTP_PORT), StreamingHttpHandler)
        with io.open('index.html', 'r') as f:
            self.index_template = f.read()
        with io.open('jsmpg.js', 'r') as f:
            self.jsmpg_content = f.read()


class StreamingWebSocket(WebSocket):
    def opened(self):
        self.send(JSMPEG_HEADER.pack(JSMPEG_MAGIC, WIDTH, HEIGHT), binary=True)


class BroadcastOutput(object):
    def __init__(self, camera):
        print('Spawning background conversion process')
        self.converter = Popen([
            'avconv',
            '-f', 'rawvideo',
            '-pix_fmt', 'yuv420p',
            '-s', '%dx%d' % camera.resolution,
            '-r', str(float(camera.framerate)),
            '-i', '-',
            '-f', 'mpeg1video',
            '-b', '800k',
            '-r', str(float(camera.framerate)),
            '-'],
            stdin=PIPE, stdout=PIPE, stderr=io.open(os.devnull, 'wb'),
            shell=False, close_fds=True)

    def write(self, b):
        self.converter.stdin.write(b)

    def flush(self):
        print('Waiting for background conversion process to exit')
        self.converter.stdin.close()
        self.converter.wait()


class BroadcastThread(Thread):
    def __init__(self, converter, websocket_server):
        super(BroadcastThread, self).__init__()
        self.converter = converter
        self.websocket_server = websocket_server

    def run(self):
        try:
            while True:
                buf = self.converter.stdout.read(512)
                if buf:
                    self.websocket_server.manager.broadcast(buf, binary=True)
                elif self.converter.poll() is not None:
                    break
        finally:
            self.converter.stdout.close()

class SensorThread(Thread):
    hora = 0
    def __init__(self):
        super(SensorThread, self).__init__()
        self.hora = 0
        self.bounceTime = 15

    def run(self):
        while True:
            config.read('config.ini')
            ACTIVE = config.get('main','active')
            STREAMING = config.get('main', 'streaming')
            SAVE = config.get('main', 'save')
            if pir.motion_detected:
                if (time() - self.hora) > self.bounceTime:
                    self.hora = time()
                    print("Motion detected at: " + localtime())
                    if ACTIVE == 'on':
                        alarm.pushMessage(CHANNEL_NAME, "Detectado movimiento en el sensor! \nBroadcast accesible en: http://<CAM Server IP>:8082/index.html \nPermiso para grabar: " + SAVE)
                        self.start_streaming()
            sleep(0.5)
    def start_streaming(self):
        config.read('config.ini')
        ACTIVE = config.get('main','active')
        SAVE = config.get('main', 'save')
        print('Initializing camera')
        with picamera.PiCamera() as camera:
            camera.resolution = (WIDTH, HEIGHT)
            camera.framerate = FRAMERATE
            sleep(1) # camera warm-up time
            print('Initializing websockets server on port %d' % WS_PORT)
            websocket_server = make_server(
                '', WS_PORT,
                server_class=WSGIServer,
                handler_class=WebSocketWSGIRequestHandler,
                app=WebSocketWSGIApplication(handler_cls=StreamingWebSocket))
            websocket_server.initialize_websockets_manager()
            websocket_thread = Thread(target=websocket_server.serve_forever)
            print('Initializing broadcast thread')
            output = BroadcastOutput(camera)
            broadcast_thread = BroadcastThread(output.converter, websocket_server)
            print('Starting recording')
            camera.start_recording(output, 'yuv')
            if SAVE == 'on':
                camera.start_recording('video.h264', splitter_port=2)
            try:
                print('Starting websockets thread')
                websocket_thread.start()
                print('Starting broadcast thread')
                broadcast_thread.start()
                while True:
                    config.read('config.ini')
                    ACTIVE = config.get('main','active')
                    SAVE = config.get('main', 'save')
                    if SAVE == 'off'
                        print("La grabacion se esta guardando")
                        camera.stop_recording(splitter_port=2)
                    if ACTIVE == 'off':
                        print('Stopping recording')
                        camera.stop_recording()
                        print('Waiting for broadcast thread to finish')
                        broadcast_thread.join()
                        #print('Shutting down HTTP server')
                        #http_server.shutdown()
                        print('Shutting down websockets server')
                        websocket_server.shutdown()
                        #print('Waiting for HTTP server thread to finish')
                        #http_thread.join()
                        print('Waiting for websockets thread to finish')
                        websocket_thread.join()
                        return
                    camera.wait_recording(1)
            except KeyboardInterrupt:
                print('Stopping recording')
                camera.stop_recording()
                print('Waiting for broadcast thread to finish')
                broadcast_thread.join()
                #print('Shutting down HTTP server')
                #http_server.shutdown()
                print('Shutting down websockets server')
                websocket_server.shutdown()
                #print('Waiting for HTTP server thread to finish')
                #http_thread.join()
                print('Waiting for websockets thread to finish')
                websocket_thread.join()

def main():
    config.read('config.ini')
    ACTIVE = config.get('main','active')
    SAVE = config.get('main', 'save')
    print("Estado del sistema: " + ACTIVE)
    print("Permiso para grabar: " + SAVE)
    print('Initializing HTTP server on port %d' % HTTP_PORT)
    http_server = StreamingHttpServer()
    http_thread = Thread(target=http_server.serve_forever)
    print('Starting HTTP server thread')
    http_thread.start()
    print('Initializing sensor thread')
    sensor_thread = SensorThread()
    print('Starting sensor thread')
    sensor_thread.start()


if __name__ == '__main__':
    main()
