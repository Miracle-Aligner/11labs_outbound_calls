import streamlit as st
import requests
import os
from dotenv import load_dotenv

load_dotenv()
HOST = os.getenv("HOST")


def make_call(name, number):
    name = name if name else ""
    number = number if number else "+13022029985"

    call_url = f"https://{HOST}/outbound-call"
    call_payload = {"name": name, "number": number}

    headers = {"Content-Type": "application/json"}
    response = requests.post(call_url, json=call_payload, headers=headers)

    return response.json()


# Streamlit UI
st.title("Call Initiator")

name = st.text_input("Enter Name (optional):", "Olena")
number = st.text_input("Enter Phone Number (optional):", "+13022029985")

if st.button("Make a Call"):
    result = make_call(name, number)

    if result and "success" in result and result["success"]:
        st.success("Call initiated successfully!")
        st.json(result)
    else:
        st.error("Failed to initiate the call.")
        st.json(result)
