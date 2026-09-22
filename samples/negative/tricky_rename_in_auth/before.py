def check_password(user, candidate_pw):
    stored_hash = user.password_hash
    return verify_hash(candidate_pw, stored_hash)
