#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <Servo.h>

// WiFi credentials
const char* ssid = "YourWiFiSSID";          // Replace with your WiFi SSID
const char* password = "YourWiFiPassword";   // Replace with your WiFi password

// MQTT Broker settings
const char* mqtt_server = "YourMQTTBrokerIP";    // Replace with your MQTT broker IP
const int mqtt_port = 1883;
const char* mqtt_user = "YourMQTTUsername";      // Replace with your MQTT username
const char* mqtt_password = "YourMQTTPassword";  // Replace with your MQTT password
const char* mqtt_topic = "home/device_commands"; // MQTT topic for device commands

// Servo settings
const int servoPin = D5;   // GPIO pin for the servo
Servo myServo;
int servoOpenPosition = 140;  // Position to open the servo
int servoClosePosition = 30;   // Position to close the servo
bool isServoOpen = false;      // Initial state of the servo

WiFiClient espClient;
PubSubClient client(espClient);

void moveServo(int targetPosition) {
  int currentPosition = myServo.read();
  if (currentPosition < targetPosition) {
    for (int pos = currentPosition; pos <= targetPosition; pos++) {
      myServo.write(pos);
      delay(20);
    }
  } else {
    for (int pos = currentPosition; pos >= targetPosition; pos--) {
      myServo.write(pos);
      delay(20);
    }
  }
}

void setup() {
  Serial.begin(115200);
  myServo.attach(servoPin);
  myServo.write(servoClosePosition);  // Start in closed position

  setup_wifi();
  client.setServer(mqtt_server, mqtt_port);
  client.setCallback(callback);
}

void setup_wifi() {
  delay(10);
  Serial.print("Connecting to WiFi...");
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\nWiFi connected, IP address: ");
  Serial.println(WiFi.localIP());
}

void callback(char* topic, byte* message, unsigned int length) {
  String messageTemp;
  for (int i = 0; i < length; i++) {
    messageTemp += (char)message[i];
  }

  // Check for variations of "halloween 1" in the message
  if (messageTemp.equalsIgnoreCase("halloween 1") || 
      messageTemp.equalsIgnoreCase("halloween1 activated") || 
      messageTemp.equalsIgnoreCase("activate halloween 1")) {
    if (isServoOpen) {
      moveServo(servoClosePosition);
      isServoOpen = false;
    } else {
      moveServo(servoOpenPosition);
      isServoOpen = true;
    }
  }
}

void reconnect() {
  while (!client.connected()) {
    if (client.connect("ESP8266Client", mqtt_user, mqtt_password)) {
      client.subscribe(mqtt_topic);
    } else {
      delay(5000);
    }
  }
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();
}
