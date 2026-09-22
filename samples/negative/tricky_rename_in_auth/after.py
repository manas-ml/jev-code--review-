def check_password(user, submitted_pw):
    stored_hash = user.password_hash
    return verify_hash(submitted_pw, stored_hash)
