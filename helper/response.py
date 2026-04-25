def success_response(data, message: str = "Success"):
    return {
        "success": True,
        "data": data,
        "message": message
    }