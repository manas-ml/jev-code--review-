def get_user_orders(request, db):
    user_id = request.args.get("user_id")
    query = f"SELECT * FROM orders WHERE user_id = {user_id}"
    return db.execute(query)
