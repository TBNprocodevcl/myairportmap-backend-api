def normalize_handle(email: str):
    return email.split("@")[0].lower()

def avatar_url_for_handle(handle: str):
    return f"https://api.dicebear.com/7.x/initials/svg?seed={handle}"