"""A `django-otp <https://django-otp-official.readthedocs.io/>`_ device that sends its codes as texts from your own
phone. Add ``"django_smstunnel.otp"`` to ``INSTALLED_APPS`` to use it, and run ``migrate``.

A text message is a weaker factor than an authenticator app or a passkey: numbers can be moved to another SIM and texts
can be intercepted. Offer it as an optional factor, never as the only one, and never as the way to recover a password.
"""
