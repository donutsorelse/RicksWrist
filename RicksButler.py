#  -*- coding: UTF-8 -*-

import sys
import speech_recognition as sr
import time
import logging
import pyttsx3
import paho.mqtt.publish as publish
from notecard import notecard
from pinpong.board import Board, Pin, SR04_URM10, Tone
import serial
from unihiker import GUI
from pinpong.extension.unihiker import button_a, button_b

# Configuration
MQTT_BROKER = "your_flask_server_ip"
MQTT_PORT = 1883
MQTT_TOPIC = "home/your_topic"
MQTT_USERNAME = "username"
MQTT_PASSWORD = "password"
NOTECARD_SERIAL_PORT = '/dev/serial0'
notecard_port = None
NOTECARD_BAUD_RATE = 9600
PRODUCT_UID = "your_product_uid"
PORT = '/dev/ttyACM0'

# Initialize Board and Sensors
board = Board()
board.begin()
ultrasonic_sensor = SR04_URM10(Pin(Pin.P0), Pin(Pin.P1))
tone = Tone(Pin(Pin.P26))

# Initialize Text-To-Speech Engine
engine = pyttsx3.init()
engine.setProperty('rate', 150)
engine.setProperty('volume', 1.0)

# Setup Logging
logging.basicConfig(level=logging.INFO)

# Initialize GUI
u_gui = GUI()

def display_command_on_screen(command):
    u_gui.clear()
    x, y = 0, 0
    max_width = 240  # Screen width limit
    font_size = 20
    words = command.split()
    line = ''

    for word in words:
        test_line = f'{line} {word}'.strip()
        text_width = len(test_line) * (font_size // 2)  # Approximate width calculation
        if text_width > max_width:
            u_gui.draw_text(x=x, y=y, w=max_width, text=line, font_size=font_size, color="#0000FF")
            y += font_size + 5  # Move to the next line
            line = word
        else:
            line = test_line

    if line:
        u_gui.draw_text(x=x, y=y, w=max_width, text=line, font_size=font_size, color="#0000FF")

# Helper Functions
def check_wifi_connection():
    try:
        import socket  # Import moved here to resolve the error
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except Exception as ex:
        logging.error(f"Wi-Fi check failed: {ex}")
        return False

def send_command_via_mqtt(command, retry_attempts=3, retry_delay=2):
    attempts = 0
    while attempts < retry_attempts:
        try:
            publish.single(MQTT_TOPIC, command, hostname=MQTT_BROKER, port=MQTT_PORT,
                           auth={'username': MQTT_USERNAME, 'password': MQTT_PASSWORD},
                           keepalive=60)
            logging.info("Command sent via MQTT over Wi-Fi.")
            return
        except Exception as e:
            logging.error(f"Failed to send command via MQTT (attempt {attempts + 1}): {e}")
            attempts += 1
            time.sleep(retry_delay)
    
    logging.error(f"All {retry_attempts} MQTT send attempts failed.")

def send_command_via_notecard(command):
    try:
        req = {"req": "note.add", "body": {"text": command}}
        rsp = notecard_port.Transaction(req)
        print(f"Alert sent: {rsp}")
    except Exception as e:
        logging.error(f"Failed to send command via Notecard: {e}")

def send_command(command):
    display_command_on_screen(command)
    if check_wifi_connection():
        send_command_via_mqtt(command)
    else:
        send_command_via_notecard(command)

def speak(text):
    engine.say(text)
    engine.runAndWait()

def setup_serial_connection(port, baud_rate):
    try:
        return serial.Serial(port, baud_rate)
    except Exception as e:
        print(f"Failed to open serial port: {e}")
        return None

def setup_notecard(serial_port):
    card = notecard.OpenSerial(serial_port)
    req = {"req": "hub.set", "product": PRODUCT_UID, "mode": "continuous"}
    rsp = card.Transaction(req)
    print(f"Setup response from Notecard: {rsp}")
    return card

def initialize_blues_service():
    global notecard_port
    if not notecard_port:
        try:
            serial_port = setup_serial_connection(PORT, NOTECARD_BAUD_RATE)
            if serial_port is not None:
                notecard_port = setup_notecard(serial_port)
            else:
                print("Failed to set up the serial port connection.")
        except Exception as e:
            speak("Failed to connect to Blues services.")
            print(f"Error: {str(e)}")

def halloween1_command():
    send_command("halloween1 activated")

def halloween2_command():
    send_command("halloween2 activated")

def main():
    recognizer = sr.Recognizer()
    microphone = sr.Microphone()

    # Adjust the recognizer sensitivity to ambient noise and record audio
    with microphone as source:
        recognizer.dynamic_energy_threshold = False  # Use a fixed energy threshold
        recognizer.energy_threshold = 300  # Adjust this value as needed
        recognizer.adjust_for_ambient_noise(source, duration=0.5)
        print("Set minimum energy threshold to:", recognizer.energy_threshold)

    recognizer.pause_threshold = 0.8
    recognizer.non_speaking_duration = 0.4

    listening_for_command = False
    accumulated_command = ''

    def callback(recognizer, audio):
        nonlocal listening_for_command, accumulated_command
        try:
            # Recognize speech
            text = recognizer.recognize_google(audio).lower()
            print("You said:", text)

            if 'butler' in text:
                listening_for_command = True
                text = text.split('butler', 1)[1].strip()
                if text:
                    accumulated_command += ' ' + text
                print("Wake word detected. Starting to accumulate command.")

            elif listening_for_command:
                accumulated_command += ' ' + text

            # Process the accumulated command
            if listening_for_command:
                print("Accumulated command:", accumulated_command.strip())
                send_command(accumulated_command.strip())
                print("Command sent.")
                listening_for_command = False
                accumulated_command = ''

        except sr.UnknownValueError:
            print("Could not understand the audio")
        except sr.RequestError as e:
            print(f"Could not request results from Speech Recognition service; {e}")
        except Exception as e:
            logging.error(f"Unhandled exception in callback: {e}")

    stop_listening = recognizer.listen_in_background(microphone, callback)

    button_a_pressed = False
    button_b_pressed = False

    try:
        while True:
            # Check if button A is pressed and wait for release before triggering again
            if button_a.is_pressed():
                if not button_a_pressed:
                    halloween1_command()
                    button_a_pressed = True
            else:
                button_a_pressed = False

            # Check if button B is pressed and wait for release before triggering again
            if button_b.is_pressed():
                if not button_b_pressed:
                    halloween2_command()
                    button_b_pressed = True
            else:
                button_b_pressed = False

            time.sleep(0.1)
    except KeyboardInterrupt:
        stop_listening(wait_for_stop=False)
        print("Stopped listening...")

if __name__ == "__main__":
    main()
