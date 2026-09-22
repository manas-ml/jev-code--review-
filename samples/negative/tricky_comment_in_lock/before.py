import threading

_lock = threading.Lock()

def withdraw(account, amount):
    with _lock:
        account.balance -= amount
