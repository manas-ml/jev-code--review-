import os

def get_api_client():
    api_key = os.environ["PAYMENTS_API_KEY"]
    return PaymentsClient(api_key)
