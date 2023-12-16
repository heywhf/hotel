import requests
import json

data = {
            'username': '222',
            'password': '222',
            'role': 'manager'
}
response = requests.post(f'http://127.0.0.1:5000/login',
                                 json=data
                                 )
token = json.loads(response.content)['token']
headers = {
            'Authorization': 'Bearer ' + token
        }

response = requests.get('http://127.0.0.1:5000/rooms',headers=headers)
print(json.loads(response.content))