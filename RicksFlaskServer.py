import os
import time
import threading
import json
import re
import pickle
import logging
from flask import Flask, request, jsonify, abort
import paho.mqtt.client as mqtt
import requests  # For Nest API
from wakeonlan import send_magic_packet  # For Wake-on-LAN
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import openai

# ------------------------- Configuration Parameters ------------------------- #

# MQTT Configuration
BROKER_ADDRESS = os.getenv('MQTT_BROKER_ADDRESS', 'your_mqtt_broker_address')  # e.g., '192.168.1.100'
MQTT_TOPIC = os.getenv('MQTT_TOPIC', 'your_mqtt_topic')                        # e.g., 'home/smartHome'
DEVICE_COMMAND_TOPIC = os.getenv('DEVICE_COMMAND_TOPIC', 'your_device_command_topic')  # e.g., 'home/device_commands'
MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'your_mqtt_username')             
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'your_mqtt_password')             

# Flask Configuration
HTTP_SERVER_PORT = int(os.getenv('HTTP_SERVER_PORT', 5000))                    # Default is 5000

# Nest API Configuration
ENTERPRISE_ID = os.getenv('ENTERPRISE_ID', 'your_enterprise_id')               
CLIENT_SECRETS_FILE = os.getenv('CLIENT_SECRETS_FILE', 'client_secrets.json') # Ensure this file is secured
SCOPES = ['https://www.googleapis.com/auth/sdm.service']
TOKEN_PICKLE = os.getenv('TOKEN_PICKLE', 'token.pickle')                       # Secure storage for tokens
DEVICE_FILE = os.getenv('DEVICE_FILE', 'devices.json')                     

# Wake-on-LAN Configuration
COMPUTER_MAC_ADDRESS = os.getenv('COMPUTER_MAC_ADDRESS', 'your_computer_mac_address')  
COMPUTER_IP_ADDRESS = os.getenv('COMPUTER_IP_ADDRESS', 'your_computer_ip_address')      

# OpenAI Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'your-openai-api-key')  # Replace with your OpenAI API key

# Desired Temperatures
DESIRED_TEMP_COOL = 70  # Default desired temperature for cooling (Fahrenheit)
TEMP_REVERT_COOL = 75    # Default temperature to revert to after recording (Fahrenheit)

# ---------------------------- Initialize Components ---------------------------- #

# Initialize OpenAI
openai.api_key = OPENAI_API_KEY

# Create Flask app
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Initialize MQTT client
mqtt_client = mqtt.Client(client_id="CM4Stack_FireAlarm")
mqtt_client.username_pw_set(username=MQTT_USERNAME, password=MQTT_PASSWORD)

# Global devices list
devices = []

# Thermostat Locations Mapping
thermostat_locations = {}
default_thermostat_id = None  # To be set after devices are loaded

# ------------------------------ Helper Functions ------------------------------ #

def authenticate_nest():
    """Handles OAuth2 authentication with the Nest API."""
    creds = None
    if os.path.exists(TOKEN_PICKLE):
        with open(TOKEN_PICKLE, 'rb') as token_file:
            creds = pickle.load(token_file)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logging.info("Refreshing access token...")
                creds.refresh(Request())
                with open(TOKEN_PICKLE, 'wb') as token_file:
                    pickle.dump(creds, token_file)
            except Exception as e:
                logging.error(f"Failed to refresh token: {e}")
                os.remove(TOKEN_PICKLE)
                creds = None
        if not creds or not creds.valid:
            logging.info("Initiating new OAuth flow...")
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE,
                SCOPES
            )
            creds = flow.run_local_server(
                host='localhost',
                port=8080,
                authorization_prompt_message='Please visit this URL: {url}',
                success_message='Authorization successful. You may close this window.',
                open_browser=True
            )
            with open(TOKEN_PICKLE, 'wb') as token_file:
                pickle.dump(creds, token_file)
    return creds

def get_nest_devices():
    """Retrieves the list of Nest devices associated with the account."""
    creds = authenticate_nest()
    access_token = creds.token
    url = f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{ENTERPRISE_ID}/devices"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        devices_data = response.json().get('devices', [])
        formatted_devices = [{
            "name": device.get("traits", {}).get("sdm.devices.traits.Info", {}).get("customName", device['name']),
            "type": device.get("type"),
            "id": device['name'],
            "controller": "google_home"
        } for device in devices_data]
        return formatted_devices
    else:
        raise Exception(f"Failed to get devices: {response.content.decode()}")

def fetch_mqtt_devices():
    """Returns a list of MQTT-controlled devices."""
    return [
        {"name": "Smart Fire Alarm", "type": "fire_alarm", "id": "smart_fire_alarm", "controller": "mqtt"},
        {"name": "Halloween 1", "type": "servo", "id": "halloween_1", "controller": "mqtt"},
        {"name": "Halloween 2", "type": "servo", "id": "halloween_2", "controller": "mqtt"}
    ]

def fetch_manual_devices():
    """Returns a list of manually configured devices."""
    return [
        # Add any manually configured devices here
    ]

def update_smart_devices():
    """Fetches and updates the list of smart devices."""
    try:
        updated_devices = []
        google_devices = get_nest_devices()
        updated_devices.extend(google_devices)

        mqtt_devices = fetch_mqtt_devices()
        updated_devices.extend(mqtt_devices)

        manual_devices = fetch_manual_devices()
        updated_devices.extend(manual_devices)

        with open(DEVICE_FILE, 'w') as f:
            json.dump(updated_devices, f, indent=4)

        load_devices()  # Reload devices into the global list and mappings
        logging.info("Devices updated successfully.")
        return {"status": "success", "message": "Devices updated successfully"}
    except Exception as e:
        logging.error(f"Failed to update devices: {e}")
        return {"status": "error", "message": f"Failed to update devices: {str(e)}"}

def load_devices():
    """Loads devices from the DEVICE_FILE and updates mappings."""
    global devices, thermostat_locations, default_thermostat_id
    if os.path.exists(DEVICE_FILE):
        with open(DEVICE_FILE, 'r') as f:
            devices = json.load(f)
    else:
        devices = []
    
    # Update thermostat locations and set default
    thermostat_locations = {}
    default_thermostat_id = None
    for device in devices:
        if device['type'] == 'THERMOSTAT':
            thermostat_locations[device['name'].lower()] = device['id']
            if not default_thermostat_id:
                default_thermostat_id = device['id']  # Set the first thermostat as default

def set_nest_temperature(device_id, temp_fahrenheit):
    """Sets the Nest thermostat temperature."""
    creds = authenticate_nest()
    access_token = creds.token
    temp_celsius = (temp_fahrenheit - 32) * 5.0 / 9.0
    url = f"https://smartdevicemanagement.googleapis.com/v1/{device_id}:executeCommand"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    payload = {
        "command": "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool",
        "params": {
            "coolCelsius": temp_celsius
        }
    }
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        logging.error(f"Failed to set Nest temperature: {response.content.decode()}")
        raise Exception(f"Failed to set Nest temperature: {response.content.decode()}")
    logging.info(f"Set Nest temperature to {temp_fahrenheit}°F for device {device_id}.")

def wake_computer():
    """Sends a Wake-on-LAN magic packet to the computer's MAC address if it's off."""
    if is_computer_on(COMPUTER_IP_ADDRESS):
        logging.info("Computer is already on. No need to send Wake-on-LAN packet.")
    else:
        logging.info("Sending Wake-on-LAN packet to wake up the computer.")
        send_magic_packet(COMPUTER_MAC_ADDRESS)

def is_computer_on(ip_address):
    """Checks if the computer is on by pinging its IP address."""
    response = os.system(f"ping -c 1 {ip_address} > /dev/null 2>&1")
    return response == 0

def schedule_recording(minutes):
    """Schedules actions for recording, including temperature adjustments and waking the computer."""
    logging.info(f"Recording will be scheduled in {minutes} minutes.")

    # Immediately set the cooling temperature to DESIRED_TEMP_COOL
    set_nest_temperature(default_thermostat_id, DESIRED_TEMP_COOL)
    logging.info(f"Temperature set to {DESIRED_TEMP_COOL}°F immediately to cool down the house.")

    # Schedule setting the thermostat to TEMP_REVERT_COOL 1 minute before recording
    if minutes > 1:
        threading.Timer((minutes - 1) * 60, set_nest_temperature, args=[default_thermostat_id, TEMP_REVERT_COOL]).start()
        logging.info(f"Temperature will be set to {TEMP_REVERT_COOL}°F in {minutes - 1} minutes to turn off AC.")
    else:
        set_nest_temperature(default_thermostat_id, TEMP_REVERT_COOL)
        logging.info(f"Recording starts in less than 1 minute, setting temperature to {TEMP_REVERT_COOL}°F immediately to turn off AC.")

    # Schedule waking up the computer 2 minutes before recording
    if minutes > 2:
        threading.Timer((minutes - 2) * 60, wake_computer).start()
        logging.info(f"Computer will wake up in {minutes - 2} minutes.")
    else:
        wake_computer()
        logging.info("Recording starts in less than 2 minutes, waking computer immediately.")

def interpret_command(command_text):
    """Interprets the command text and returns an action and parameters."""
    command_text = command_text.lower().strip()

    command_patterns = [
        # Wake computer
        (r'\b(wake|turn on|start|boot up|power up)\s+(the\s+|my\s+)?(computer|pc|laptop)\b', 'wake_computer', {}),
     
        # Set temperature with location
        (r'\b(set|adjust|change|make)\s+(the\s+)?(temperature|thermostat)\s+(in\s+(the\s+)?)?(?P<location>\w+)\s*(to\s+)?(?P<temperature>\d+)\b', 
         'set_temperature', {'location': str, 'temperature': int}),

        (r'\b(make)\s+it\s+(?P<temperature>\d+)\s+degrees?\s+in\s+(the\s+)?(?P<location>\w+)\b', 
         'set_temperature', {'temperature': int, 'location': str}),

        # Set temperature without location
        (r'\b(set|adjust|change|make)\s+(the\s+)?(temperature|thermostat)\s*(to\s+)?(?P<temperature>\d+)\b',
         'set_temperature', {'temperature': int}),
        (r'\b(make)\s+it\s+(?P<temperature>\d+)\s+degrees?\b', 
         'set_temperature', {'temperature': int}),

        # Schedule recording
        (r'\b(start|begin)\s+(a\s+)?recording\s+(in\s+)?(?P<time_phrase>.*)', 
         'schedule_recording', {'time_phrase': str}),
        (r'\b(record|snooze|stop)\s+(in\s+)?(?P<time_phrase>.*)', 
         'schedule_recording', {'time_phrase': str}),

        # Update smart devices
        (r'\b(update)\s+(smart\s+)?devices\b', 
         'update_smart_devices', {}),
    ]

    for pattern, action_name, params_info in command_patterns:
        match = re.search(pattern, command_text)
        if match:
            parameters = {}
            for param_name, param_type in params_info.items():
                value = match.group(param_name)
                if value:
                    if param_type == int:
                        parameters[param_name] = int(value)
                    elif param_type == str:
                        parameters[param_name] = value.strip()
            return action_name, parameters

    return None, {}

def extract_command_text(data):
    """Extracts the command text from the incoming data."""
    if not data:
        return None

    if 'command' in data:
        return data['command']

    if 'text' in data:
        return data['text']

    if 'body' in data and 'text' in data['body']:
        return data['body']['text']

    if 'queryResult' in data:
        return data['queryResult'].get('queryText') or data['queryResult'].get('intent', {}).get('displayName')

    return None

def format_devices_for_prompt(devices):
    """Formats device information for the AI prompt."""
    device_descriptions = []
    for device in devices:
        device_descriptions.append(f"{device['name']} (ID: {device['id']}), Type: {device['type']}")
    return "\n".join(device_descriptions)

def generate_ai_response(prompt):
    """Generates a response from OpenAI's GPT-4 based on the prompt."""
    try:
        device_info = format_devices_for_prompt(devices)
        system_prompt = (
            "You are a smart home assistant. "
            "Your task is to interpret user commands and generate actions to control smart home devices. "
            "Here is a list of available devices:\n"
            f"{device_info}\n\n"
            "When you respond, you must provide a JSON object with the following structure:\n"
            "{\n"
            '  "action": "action_name",\n'
            '  "device_id": "device_id",\n'
            '  "parameters": { "param1": value1, "param2": value2 }\n'
            "}\n"
            "Do not include any extra text or explanations."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]

        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=messages,
            temperature=0.7,
            max_tokens=150
        )

        ai_response = response.choices[0].message.content.strip()
        logging.info(f"AI Response: {ai_response}")
        return ai_response
    except Exception as e:
        logging.error(f"Error generating AI response: {e}", exc_info=True)
        return None

def execute_action(action, device_id, parameters):
    """Executes the specified action on the given device."""
    device = next((d for d in devices if d['id'] == device_id), None)

    if not device:
        logging.warning(f"Device with ID '{device_id}' not found. Treating as custom device.")
        dispatch_unrecognized_command(f"{action} {device_id}")
        return

    if device["controller"] == "mqtt":
        if action == "activate":
            mqtt_client.publish(f"home/{device['id']}", "activate")
            logging.info(f"Activated device {device['name']} via MQTT.")
        else:
            logging.error(f"Unknown action '{action}' for MQTT controller.")
            dispatch_unrecognized_command(f"{action} {device_id}")
    elif device["controller"] == "google_home":
        if action == "set_temperature":
            temperature = parameters.get('temperature')
            if temperature:
                set_nest_temperature(device['id'], temperature)
                logging.info(f"Set temperature to {temperature}°F for device {device['name']}.")
        else:
            logging.error(f"Unknown action '{action}' for Google Home controller.")
            dispatch_unrecognized_command(f"{action} {device_id}")
    else:
        logging.error(f"Unknown controller '{device['controller']}' for device '{device['name']}'.")
        dispatch_unrecognized_command(f"{action} {device_id}")

def dispatch_unrecognized_command(command_text):
    """Forwards unrecognized commands to devices via MQTT."""
    mqtt_client.publish(DEVICE_COMMAND_TOPIC, command_text)
    logging.info(f"Forwarded unrecognized command to devices: {command_text}")

# ------------------------------ Flask Routes ------------------------------ #

@app.before_request
def enforce_post_json():
    """Ensures that all incoming requests are POST and contain JSON."""
    if request.path == '/oauth2callback':
        return
    if request.method != 'POST':
        logging.warning(f"Method {request.method} is not allowed.")
        abort(405)  # Method Not Allowed
    if not request.is_json:
        logging.warning(f"Invalid content type: {request.content_type}. Only JSON is allowed.")
        abort(400)  # Bad Request for non-JSON requests

@app.route('/oauth2callback', methods=['GET'])
def oauth2callback():
    """Handles the OAuth2 callback."""
    return "Authorization successful. You may close this window.", 200

@app.route('/update_devices', methods=['POST'])
def update_devices_route():
    """Endpoint to update the list of smart devices."""
    result = update_smart_devices()
    return jsonify(result)

@app.route('/command', methods=['POST'])
def handle_command():
    """Endpoint to handle incoming commands."""
    try:
        data = request.get_json()
        logging.info(f"Parsed JSON data: {data}")
        command_text = extract_command_text(data)
        logging.info(f"Processing command text: {command_text}")
        if not command_text:
            logging.warning("No command text found in the request.")
            return jsonify({"status": "error", "message": "No command text found."}), 400

        command_text = command_text.strip()
        if command_text.lower().startswith('ai '):
            # AI command
            ai_prompt = command_text[3:].strip()
            ai_response = generate_ai_response(ai_prompt)
            if not ai_response:
                return jsonify({"status": "error", "message": "Failed to generate AI response."}), 500

            try:
                ai_data = json.loads(ai_response)
                action = ai_data.get('action')
                device_id = ai_data.get('device_id')
                params = ai_data.get('parameters', {})

                if not action or not device_id:
                    logging.error("Invalid AI response format.")
                    return jsonify({"status": "error", "message": "Invalid AI response format."}), 500

                execute_action(action, device_id, params)
                return jsonify({"message": f"Action '{action}' executed on device '{device_id}'."}), 200

            except json.JSONDecodeError as e:
                logging.error(f"Failed to parse AI response: {e}")
                return jsonify({"status": "error", "message": "Failed to parse AI response."}), 500
        else:
            # Non-AI command
            action, parameters = interpret_command(command_text)
            if action:
                execute_action(action, parameters.get('device_id'), parameters)
                return jsonify({"message": f"Action '{action}' executed."}), 200
            else:
                logging.info("Command not recognized, forwarding to listening devices.")
                dispatch_unrecognized_command(command_text)
                return jsonify({"status": "forwarded", "message": "Command forwarded to devices."}), 200

    except Exception as e:
        logging.error(f"Failed to process command: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Failed to process command due to an internal error."}), 500

# ---------------------------- AI Integration ---------------------------- #

def format_devices_for_prompt(devices):
    """Formats device information for the AI prompt."""
    device_descriptions = []
    for device in devices:
        device_descriptions.append(f"{device['name']} (ID: {device['id']}), Type: {device['type']}")
    return "\n".join(device_descriptions)

def generate_ai_response(prompt):
    """Generates a response from OpenAI's GPT-4o based on the prompt."""
    try:
        device_info = format_devices_for_prompt(devices)
        system_prompt = (
            "You are a smart home assistant. "
            "Your task is to interpret user commands and generate actions to control smart home devices. "
            "Here is a list of available devices:\n"
            f"{device_info}\n\n"
            "When you respond, you must provide a JSON object with the following structure:\n"
            "{\n"
            '  "action": "action_name",\n'
            '  "device_id": "device_id",\n'
            '  "parameters": { "param1": value1, "param2": value2 }\n'
            "}\n"
            "Do not include any extra text or explanations."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]

        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=150
        )

        ai_response = response.choices[0].message.content.strip()
        logging.info(f"AI Response: {ai_response}")
        return ai_response
    except Exception as e:
        logging.error(f"Error generating AI response: {e}", exc_info=True)
        return None

# ----------------------------- Main Execution ----------------------------- #

def setup_mqtt():
    """Sets up the MQTT client and starts the loop."""
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(BROKER_ADDRESS)
        mqtt_client.loop_start()
        logging.info("MQTT client loop started.")
    except Exception as e:
        logging.error(f"Failed to connect to MQTT Broker: {e}")

def main():
    """Main function to run the Flask server and initialize components."""
    load_devices()
    setup_mqtt()
    try:
        app.run(host='0.0.0.0', port=HTTP_SERVER_PORT)
    except Exception as e:
        logging.error(f"Exception occurred: {e}")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

if __name__ == '__main__':
    main()
