def generate_user_id(counter: int) -> str:
    """
    Generate a user ID in the format UID-XXXX.
    
    Args:
        counter: The numeric part of the ID (will be zero-padded to 4 digits)
    
    Returns:
        A string in the format 'UID-XXXX' (e.g., 'UID-0001')
    """
    return f"UID-{counter:04d}"


# Track the last generated ID
_current_counter = 0


def get_next_user_id() -> str:
    """
    Generate the next sequential user ID.
    
    Returns:
        The next user ID in sequence (e.g., 'UID-0001', 'UID-0002', etc.)
    """
    global _current_counter
    _current_counter += 1
    return generate_user_id(_current_counter)


def reset_counter(value: int = 0) -> None:
    """
    Reset the counter to a specific value.
    
    Args:
        value: The value to reset the counter to (default: 0)
    """
    global _current_counter
    _current_counter = value
