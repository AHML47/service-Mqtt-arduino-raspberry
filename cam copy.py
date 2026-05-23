#!/usr/bin/env python3

import io
import json
import os
import threading

from datetime import datetime
from http import server
from threading import Condition
from urllib.parse import urlparse, parse_qs

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
from libcamera import controls


PHOTO_DIR = "photos"


PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Pi Camera 3 Stream</title>

<style>

body{
    background:#111;
    color:#eee;
    font-family:Arial;
    margin:0;
    padding:20px;
}

.container{
    max-width:900px;
    margin:auto;
}

img{
    width:100%;
    border:2px solid #333;
    border-radius:10px;
    background:black;
}

.panel{
    margin-top:20px;
    background:#1d1d1d;
    padding:15px;
    border-radius:10px;
}

input[type=range]{
    width:100%;
}

button{
    margin-top:10px;
    padding:12px 18px;
    border:none;
    border-radius:8px;
    cursor:pointer;
}

#captureStatus{
    margin-top:10px;
}

</style>

</head>

<body>

<div class="container">

<h1>Raspberry Pi Camera 3 Live Stream</h1>

<img src="/stream.mjpg">

<div class="panel">

<h3>Focus</h3>

<input
type="range"
id="focus"
min="0"
max="10"
step="0.1"
value="1">

<div id="focusValue">1.0</div>

<button onclick="autoFocus()">
Auto Focus
</button>

<button onclick="manualMode()">
Manual
</button>

<button onclick="continuousMode()">
Continuous AF
</button>

</div>


<div class="panel">

<h3>Capture Photo</h3>

<button onclick="capturePhoto()">
📸 Capture
</button>

<div id="captureStatus"></div>

</div>


<div class="panel">

<h3>Zoom</h3>

<input
type="range"
id="zoom"
min="1"
max="8"
step="0.5"
value="1">

<div id="zoomValue">1.0x</div>

</div>

</div>

<script>

const focusSlider =
document.getElementById("focus");

const zoomSlider =
document.getElementById("zoom");

const focusValue =
document.getElementById("focusValue");

const zoomValue =
document.getElementById("zoomValue");


focusSlider.addEventListener(
"input",
async ()=>{

const value=focusSlider.value;

focusValue.innerText=value;

await fetch(
"/focus?value="+value
);

});


zoomSlider.addEventListener(
"input",
async ()=>{

const value=zoomSlider.value;

zoomValue.innerText=value+"x";

await fetch(
"/zoom?value="+value
);

});


async function autoFocus(){

await fetch(
"/af_trigger"
);

}


async function manualMode(){

await fetch(
"/af_mode?mode=manual"
);

}


async function continuousMode(){

await fetch(
"/af_mode?mode=continuous"
);

}


async function capturePhoto(){

const status =
document.getElementById(
"captureStatus"
);

status.innerText =
"Capturing...";

try{

const res =
await fetch(
"/capture"
);

const data =
await res.json();

status.innerText =
"Saved: "+data.file;

}
catch{

status.innerText =
"Capture failed";

}

}

</script>

</body>
</html>
"""


class StreamingOutput(io.BufferedIOBase):

    def __init__(self):

        self.frame = None
        self.condition = Condition()

    def write(self, buf):

        with self.condition:

            self.frame = bytes(buf)

            self.condition.notify_all()

        return len(buf)


class CameraState:

    def __init__(self, picam2):
        self.picam2 = picam2
        self.lock = threading.Lock()
        self.zoom = 1.0
        self.lens_position = 1.0
    
        self.full_crop = (
            picam2.camera_properties["ScalerCropMaximum"]
        )
    
        self.picam2.set_controls({
            "AfMode": controls.AfModeEnum.Manual,
            "LensPosition": self.lens_position,
            "ScalerCrop": self.full_crop  # ← pin to full sensor on start
        })

    def apply_zoom(self):

        x, y, w, h = self.full_crop

        z = max(
            1.0,
            self.zoom
        )

        new_w = int(w / z)
        new_h = int(h / z)

        crop = (

            x + (w-new_w)//2,

            y + (h-new_h)//2,

            new_w,

            new_h

        )

        self.picam2.set_controls({

            "ScalerCrop":
            crop

        })

    def set_focus(self, value):

        try:

            value = float(value)

        except:

            return

        with self.lock:

            self.lens_position = value

            self.picam2.set_controls({

                "AfMode":
                controls.AfModeEnum.Manual,

                "LensPosition":
                value

            })

    def set_zoom(self, value):

        try:

            value = float(value)

        except:

            return

        self.zoom = value

        self.apply_zoom()

    def autofocus(self):

        try:

            self.picam2.set_controls({

                "AfMode":
                controls.AfModeEnum.Auto

            })

            self.picam2.autofocus_cycle()

        except Exception as e:

            print(e)

    def set_af_mode(self, mode):

        if mode == "continuous":

            self.picam2.set_controls({

                "AfMode":
                controls.AfModeEnum.Continuous

            })

        else:

            self.picam2.set_controls({

                "AfMode":
                controls.AfModeEnum.Manual,

                "LensPosition":
                self.lens_position

            })


def capture_photo(picam2):

    os.makedirs(

        PHOTO_DIR,

        exist_ok=True

    )

    filename = datetime.now().strftime(

        "%Y%m%d_%H%M%S.jpg"

    )

    path = os.path.join(

        PHOTO_DIR,

        filename

    )

    try:

        request = picam2.capture_request()

        request.save(
            "main",
            path
        )

        request.release()

        print(
            "Saved:",
            path
        )

        return filename

    except Exception as e:

        print(
            e
        )

        return "error"


state = None
output = None


class StreamingHandler(
server.BaseHTTPRequestHandler
):

    def do_GET(self):

        global state
        global output

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        query = parse_qs(
            parsed.query
        )

        if path == "/":

            self.send_response(301)

            self.send_header(
                "Location",
                "/index.html"
            )

            self.end_headers()

        elif path == "/index.html":

            body = PAGE.encode()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html"
            )

            self.send_header(
                "Content-Length",
                len(body)
            )

            self.end_headers()

            self.wfile.write(
                body
            )

        elif path == "/stream.mjpg":

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=FRAME"
            )

            self.end_headers()

            try:

                while True:

                    with output.condition:

                        output.condition.wait()

                        frame = output.frame

                    self.wfile.write(
                        b"--FRAME\r\n"
                    )

                    self.wfile.write(
                        b"Content-Type:image/jpeg\r\n\r\n"
                    )

                    self.wfile.write(
                        frame
                    )

                    self.wfile.write(
                        b"\r\n"
                    )

            except:

                pass

        elif path == "/focus":

            state.set_focus(

                query.get(
                    "value",
                    ["1"]
                )[0]

            )

            self.send_ok()

        elif path == "/zoom":

            state.set_zoom(

                query.get(
                    "value",
                    ["1"]
                )[0]

            )

            self.send_ok()

        elif path == "/af_trigger":

            state.autofocus()

            self.send_ok()

        elif path == "/af_mode":

            state.set_af_mode(

                query.get(
                    "mode",
                    ["manual"]
                )[0]

            )

            self.send_ok()

        elif path == "/capture":

            filename = capture_photo(
                state.picam2
            )

            body = json.dumps({

                "status":
                "ok",

                "file":
                filename

            }).encode()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.send_header(
                "Content-Length",
                len(body)
            )

            self.end_headers()

            self.wfile.write(
                body
            )

        else:

            self.send_error(404)

    def send_ok(self):

        body = b'{"status":"ok"}'

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            len(body)
        )

        self.end_headers()

        self.wfile.write(body)

    def log_message(
        self,
        *args
    ):

        pass


class StreamingServer(
server.ThreadingHTTPServer
):

    allow_reuse_address = True

    daemon_threads = True


def main():

    global state
    global output

    picam2 = Picamera2()

    config = picam2.create_video_configuration(
    main={
        "size": (1280, 720),
        "format": "RGB888"
    },
    raw={
        "size": (4608, 2592)  # ← forces full sensor readout = full FOV
    }
)

    picam2.configure(
        config
    )

    output = (
        StreamingOutput()
    )

    encoder = (
        MJPEGEncoder()
    )

    picam2.start_recording(

        encoder,

        FileOutput(
            output
        )

    )

    state = (
        CameraState(
            picam2
        )
    )

    print()
    print(
        "Open:"
    )

    print(
        "http://YOUR_PI_IP:8000"
    )

    StreamingServer(

        ("", 8000),

        StreamingHandler

    ).serve_forever()


if __name__ == "__main__":
    main()
