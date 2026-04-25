from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from app.core.config import settings


def send_reset_email(to_email: str, reset_link: str):
    print(f"Sending reset email to {to_email} with link: {reset_link}")
    message = Mail(
        from_email=settings.EMAIL_FROM,
        to_emails=to_email,
        subject="Reset your password",
        html_content=f"""
        <div style="font-family: Arial, sans-serif; padding: 20px;">
            <h2>Reset Password</h2>
            <p>Click button below to reset your password:</p>

            <a href="{reset_link}" target="_blank"
            style="
                    display: inline-block;
                    padding: 12px 20px;
                    background-color: #4CAF50;
                    color: white;
                    text-decoration: none;
                    border-radius: 5px;
            ">
                Reset Password
            </a>

            <p style="margin-top:20px;">Or copy this link:</p>
            <p style="word-break: break-all;">
                {reset_link}
            </p>

            <p style="font-size:12px; color:gray;">
                This link expires in 15 minutes.
            </p>
        </div>
        """
    )

    try:
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        response = sg.send(message)

        print("STATUS:", response.status_code)
        print("BODY:", response.body)
        print("HEADERS:", response.headers)

    except Exception as e:
        print("Send email error:", str(e))

def send_otp_email(to_email: str, otp: str):
    print(f"Sending OTP to {to_email}: {otp}")

    message = Mail(
        from_email=settings.EMAIL_FROM,
        to_emails=to_email,
        subject="Your OTP Code",
        html_content=f"""
        <div style="font-family: Arial, sans-serif; padding: 20px;">
            <h2>Verify your request</h2>

            <p>Your OTP code is:</p>

            <div style="
                font-size: 28px;
                font-weight: bold;
                letter-spacing: 4px;
                margin: 20px 0;
                color: #333;
            ">
                {otp}
            </div>

            <p>This code will expire in <b>5 minutes</b>.</p>

            <p style="font-size:12px; color:gray;">
                If you didn’t request this, you can ignore this email.
            </p>
        </div>
        """
    )

    try:
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        response = sg.send(message)

        print("STATUS:", response.status_code)
    except Exception as e:
        print("Send OTP email error:", str(e))