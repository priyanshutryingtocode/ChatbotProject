import openai

openai.api_key = 'YOUR_API_KEY'

try:
    response = openai.models.list()
    print("API Key is working. Available models:")
    for model in response.data:
        print(f"- {model.id}")
except openai.APIError as e:
    print(f"API Key is NOT working. Error: {e}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")