import json

import requests


BASE = "http://localhost:8000/rpc/"

r1 = requests.post(BASE, json={
    "jsonrpc": "2.0",
    "method": "otp.send",
    "params": {"chat_id": "8643951926"},
    "params": {"chat_id": "8643951926"},
    "id": 1
})

data1 = r1.json()
print(json.dumps(data1, indent=2))

otp = data1.get("result", {}).get("otp")

r2 = requests.post(BASE, json={
    "jsonrpc": "2.0",
    "method": "transfer.create",
    "params": {
        "ext_id": "abc141",
        "from_card": "8600 1234 5678 9012",
        "to_card": "9860 9876 5432 1098",
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