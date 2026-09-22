import threading

_lock = threading.Lock()

def withdraw(account, amount):
    with _lock:
        # critical section: balance must be updated under the lock
        account.balance -= amount
