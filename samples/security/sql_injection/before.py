def get_user_orders(request, db):
    user_id = request.args.get("user_id")
    query = "SELECT * FROM orders WHERE user_id = %s"
    return db.execute(query, (user_id,))
