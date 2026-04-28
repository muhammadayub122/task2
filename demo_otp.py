import json

import requests


BASE = "http://localhost:8000/rpc/"

# 1. Send OTP
r1 = requests.post(BASE, json={
    "jsonrpc": "2.0",
    "method": "otp.send",
    "params": {"chat_id": "8643951926"},
    "id": 1
})
data1 = r1.json()
print("Step 1 - otp.send response:")
print(json.dumps(data1, indent=2))

otp = data1.get("result", {}).get("otp")
if not otp:
    print("\nERROR: OTP not returned. Check server logs.")
    exit(1)

print(f"\n>>> OTP received: {otp}")
print(">>> Now using this OTP in transfer.create...\n")

# 2. Create transfer with the OTP
r2 = requests.post(BASE, json={
    "jsonrpc": "2.0",
    "method": "transfer.create",
    "params": {
        "ext_id": "abc141",
        "from_card": "9860010138878433",
        "to_card": "1779912989738185",
        "amount": 10000,
        "currency": 643,
        "sender_card_expiry": "2027-04-01",
        "chat_id": "8643951926",
        "user_otp": otp
    },
    "id": 2
})
data2 = r2.json()
print("Step 2 - transfer.create response:")
print(json.dumps(data2, indent=2))

if data2.get("result", {}).get("confirmed"):
    print("\n✅ SUCCESS! Transfer was created and auto-confirmed!")
else:
    print("\n❌ Transfer failed:", data2.get("error", {}).get("message"))