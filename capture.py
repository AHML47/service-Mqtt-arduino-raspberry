#!/usr/bin/env python3

from service.photo_capture import PhotoCaptureError, PhotoCaptureService


def main():
    capture = PhotoCaptureService(output_dir=".")

    try:
        result = capture.capture()
    except PhotoCaptureError as exc:
        print("Photo capture failed:", exc)
        raise SystemExit(1) from exc

    print()
    print("===================================")
    print("Photo captured successfully")
    print("Saved as:", result.filename)
    print("===================================")
    print()


if __name__ == "__main__":
    main()