# Copyright 2014 SolidBuilds.com. All rights reserved
#
# Authors: Ling Thio <ling.thio@gmail.com>


from flask import redirect, render_template, render_template_string, session
from flask import current_app, flash, redirect, render_template, request, url_for
from flask_user import current_user, login_required
from flask_login import login_user, logout_user
from app.pages.date import *
from app.pages.encryption import decode

from flask_user import signals
from flask_user.translations import gettext as _

from app.app_and_db import app, db
from app.users.forms import UserProfileForm
import threading

import stripe

#
# User Profile form
#
def confirm_email(token):
    """ Verify email confirmation token and activate the user account."""
    # Verify token
    user_manager = current_app.user_manager
    db_adapter = user_manager.db_adapter
    is_valid, has_expired, object_id = user_manager.verify_token(
            token,
            user_manager.confirm_email_expiration)

    if has_expired:
        flash(_('Your confirmation token has expired.'), 'error')
        return redirect(url_for('user.login'))

    if not is_valid:
        flash(_('Invalid confirmation token.'), 'error')
        return redirect(url_for('user.login'))

    # Confirm email by setting User.active=True and User.confirmed_at=utcnow()
    if db_adapter.UserEmailClass:
        user_email = user_manager.get_user_email_by_id(object_id)
        if user_email:
            user_email.confirmed_at = datetime.utcnow()
            user = user_email.user
        else:
            user = None
    else:
        user_email = None
        user = user_manager.get_user_by_id(object_id)
        if user:
            user.confirmed_at = datetime.utcnow()

    if user:
        user.set_active(True)
        db_adapter.commit()
    else:                                               # pragma: no cover
        flash(_('Invalid confirmation token.'), 'error')
        return redirect(url_for('user.login'))

    # Send email_confirmed signal
    signals.user_confirmed_email.send(current_app._get_current_object(), user=user)

    # Prepare one-time system message
    flash(_('Your email has been confirmed.'), 'success')

    # Auto-login after confirm or redirect to login page
    next = request.args.get('next', _endpoint_url(user_manager.after_confirm_endpoint))
    if user_manager.auto_login_after_confirm:
        return _do_login_user(user, next)                       # auto-login
    else:
        return redirect(url_for('user.login')+'?next='+next)    # redirect to login page


@login_required
def change_password():
    """ Prompt for old password and new password and change the user's password."""
    user_manager =  current_app.user_manager
    db_adapter = user_manager.db_adapter

    # Initialize form
    form = user_manager.change_password_form(request.form)
    form.next.data = request.args.get('next', _endpoint_url(user_manager.after_change_password_endpoint))  # Place ?next query param in next form field

    # Process valid POST
    if request.method=='POST' and form.validate():
        # Hash password
        hashed_password = user_manager.hash_password(form.new_password.data)

        # Change password
        user_manager.update_password(current_user, hashed_password)

        # Send 'password_changed' email
        if user_manager.enable_email and user_manager.send_password_changed_email:
            emails.send_password_changed_email(current_user)

        # Send password_changed signal
        signals.user_changed_password.send(current_app._get_current_object(), user=current_user)

        # Prepare one-time system message
        flash(_('Your password has been changed successfully.'), 'success')

        # Redirect to 'next' URL
        return redirect(form.next.data)

    # Process GET or invalid POST
    return render_template(user_manager.change_password_template, form=form)

@login_required
def change_username():
    """ Prompt for new username and old password and change the user's username."""
    user_manager =  current_app.user_manager
    db_adapter = user_manager.db_adapter

    # Initialize form
    form = user_manager.change_username_form(request.form)
    form.next.data = request.args.get('next', _endpoint_url(user_manager.after_change_username_endpoint))  # Place ?next query param in next form field

    # Process valid POST
    if request.method=='POST' and form.validate():
        new_username = form.new_username.data

        # Change username
        user_auth = current_user.user_auth if db_adapter.UserAuthClass and hasattr(current_user, 'user_auth') else current_user
        db_adapter.update_object(user_auth, username=new_username)
        db_adapter.commit()

        # Send 'username_changed' email
        if user_manager.enable_email and user_manager.send_username_changed_email:
            emails.send_username_changed_email(current_user)

        # Send username_changed signal
        signals.user_changed_username.send(current_app._get_current_object(), user=current_user)

        # Prepare one-time system message
        flash(_("Your username has been changed to '%(username)s'.", username=new_username), 'success')

        # Redirect to 'next' URL
        return redirect(form.next.data)

    # Process GET or invalid POST
    return render_template(user_manager.change_username_template, form=form)

@login_required
def email_action(id, action):
    """ Perform action 'action' on UserEmail object 'id'
    """
    user_manager =  current_app.user_manager
    db_adapter = user_manager.db_adapter

    # Retrieve UserEmail by id
    user_email = db_adapter.find_first_object(db_adapter.UserEmailClass, id=id)

    # Users may only change their own UserEmails
    if not user_email or user_email.user_id != int(current_user.get_id()):
        return unauthorized()

    if action=='delete':
        # Primary UserEmail can not be deleted
        if user_email.is_primary:
            return unauthorized()
        # Delete UserEmail
        db_adapter.delete_object(user_email)
        db_adapter.commit()

    elif action=='make-primary':
        # Disable previously primary emails
        user_emails = db_adapter.find_all_objects(db_adapter.UserEmailClass, user_id=int(current_user.get_id()))
        for ue in user_emails:
            if ue.is_primary:
                ue.is_primary = False
        # Enable current primary email
        user_email.is_primary = True
        # Commit
        db_adapter.commit()

    elif action=='confirm':
        _send_confirm_email(user_email.user, user_email)

    else:
        return unauthorized()

    return redirect(url_for('user.manage_emails'))

def forgot_password():
    """Prompt for email and send reset password email."""
    user_manager =  current_app.user_manager
    db_adapter = user_manager.db_adapter

    # Initialize form
    form = user_manager.forgot_password_form(request.form)

    # Process valid POST
    if request.method=='POST' and form.validate():
        email = form.email.data

        # Find user by email
        user, user_email = user_manager.find_user_by_email(email)
        if user:
            # Generate reset password link
            token = user_manager.generate_token(int(user.get_id()))
            reset_password_link = url_for('user.reset_password', token=token, _external=True)

            # Send forgot password email
            emails.send_forgot_password_email(user, user_email, reset_password_link)

            # Store token
            if hasattr(user, 'reset_password_token'):
                db_adapter.update_object(user, reset_password_token=token)
                db_adapter.commit()

            # Send forgot_password signal
            signals.user_forgot_password.send(current_app._get_current_object(), user=user)

        # Prepare one-time system message
        flash(_("A reset password email has been sent to '%(email)s'. Open that email and follow the instructions to reset your password.", email=email), 'success')

        # Redirect to the login page
        return redirect(_endpoint_url(user_manager.after_forgot_password_endpoint))

    # Process GET or invalid POST
    return render_template(user_manager.forgot_password_template, form=form)

@app.route('/user/profile', methods=['GET', 'POST'])
@login_required
def user_profile_page():
    # Initialize form
    form = UserProfileForm(request.form, current_user)

    # Process valid POST
    if request.method=='POST' and form.validate():

        # Copy form fields to user_profile fields
        form.populate_obj(current_user)

        # Save user_profile
        db.session.commit()

        # Redirect to home page
        return redirect(url_for('home_page'))

    # Process GET or invalid POST
    return render_template('users/user_profile_page.html',form=form)

@app.route('/user/sign-in', methods=['GET', 'POST'])
def login():
    """ Prompt for username/email and password and sign the user in."""
    user_manager =  current_app.user_manager
    db_adapter = user_manager.db_adapter

    next = request.args.get('next', _endpoint_url(user_manager.after_login_endpoint))
    reg_next = request.args.get('reg_next', _endpoint_url(user_manager.after_register_endpoint))

    # Immediately redirect already logged in users
    if current_user.is_authenticated and user_manager.auto_login_at_login:
        return redirect(next)

    # Initialize form
    login_form = user_manager.login_form(request.form)          # for login.html
    register_form = user_manager.register_form()                # for login_or_register.html
    if request.method!='POST':
        login_form.next.data     = register_form.next.data = next
        login_form.reg_next.data = register_form.reg_next.data = reg_next

    # Process valid POST
    if request.method=='POST' and login_form.validate():
        # Retrieve User
        user = None
        user_email = None
        if user_manager.enable_username:
            # Find user record by username
            user = user_manager.find_user_by_username(login_form.username.data)
            user_email = None
            # Find primary user_email record
            if user and db_adapter.UserEmailClass:
                user_email = db_adapter.find_first_object(db_adapter.UserEmailClass,
                        user_id=int(user.get_id()),
                        is_primary=True,
                        )
            # Find user record by email (with form.username)
            if not user and user_manager.enable_email:
                user, user_email = user_manager.find_user_by_email(login_form.username.data)
        else:
            # Find user by email (with form.email)
            user, user_email = user_manager.find_user_by_email(login_form.email.data)

        if user:
            # Log user in

            return _do_login_user(user, login_form.next.data, login_form.remember_me.data)

    # Process GET or invalid POST
    return render_template(user_manager.login_template,
            form=login_form,
            login_form=login_form,
            register_form=register_form)

@app.route('/user/sign-out', methods=['GET', 'POST'])
def logout():

    """ Sign the user out."""
    user_manager =  current_app.user_manager

    # Send user_logged_out signal
    signals.user_logged_out.send(current_app._get_current_object(), user=current_user)

    # Use Flask-Login to sign out user
    logout_user()
    # Prepare one-time system message
    flash(_('You have signed out successfully.'), 'success')

    # Redirect to logout_next endpoint or '/'
    next = request.args.get('next', _endpoint_url(user_manager.after_logout_endpoint))  # Get 'next' query param
    return redirect(next)

def _do_login_user(user, next, remember_me=False):
    # User must have been authenticated
    if not user: return unauthenticated()

    # Check if user account has been disabled
    if not user.is_active():
        flash(_('Your account has not been activated. Please check your email.'), 'error')
        return redirect(url_for('user.login'))

    # Check if user has a confirmed email address
    user_manager = current_app.user_manager
    if user_manager.enable_email and user_manager.enable_confirm_email \
            and not current_app.user_manager.enable_login_without_confirm_email \
            and not user.has_confirmed_email():
        url = url_for('user.resend_confirm_email')
        flash(_('Your email address has not yet been confirmed. <a href="%(url)s">Re-send confirmation email</a>.', url=url), 'error')
        return redirect(url_for('user.login'))

    # Use Flask-Login to sign in user
    #print('login_user: remember_me=', remember_me)
    login_user(user, remember=remember_me)

    # Send user_logged_in signal
    signals.user_logged_in.send(current_app._get_current_object(), user=user)

    # Prepare one-time system message
    #flash(_('You have signed in successfully.'), 'success')
    if current_user.user_auth.credentials == 1:
        stripe.api_key = decode(current_user.user_auth.api_key)
        session['api_key'] = stripe.api_key

        # Redirect to 'next' URL
        return redirect(next)
    else:
        flash(_('Please enter your Stripe API key'), 'error')
        return redirect(url_for('getstarted'))

def register():
    """ Display registration form and create new User."""
    user_manager =  current_app.user_manager
    db_adapter = user_manager.db_adapter

    next = request.args.get('next', _endpoint_url(user_manager.after_login_endpoint))
    reg_next = request.args.get('reg_next', _endpoint_url(user_manager.after_register_endpoint))

    # Initialize form
    login_form = user_manager.login_form()                      # for login_or_register.html
    register_form = user_manager.register_form(request.form)    # for register.html
    if request.method!='POST':
        login_form.next.data     = register_form.next.data     = next
        login_form.reg_next.data = register_form.reg_next.data = reg_next

    # Process valid POST
    if request.method=='POST' and register_form.validate():

        # Create a User object using Form fields that have a corresponding User field
        User = db_adapter.UserClass
        user_class_fields = User.__dict__
        user_fields = {}
        user_auth_fi

        # Create a UserEmail object using Form fields that have a corresponding UserEmail field
        if db_adapter.UserEmailClass:
            UserEmail = db_adapter.UserEmailClass
            user_email_class_fields = UserEmail.__dict__
            user_email_fields = {}

        # Create a UserAuth object using Form fields that have a corresponding UserAuth field
        if db_adapter.UserAuthClass:
            UserAuth = db_adapter.UserAuthClass
            user_auth_class_fields = UserAuth.__dict__
            user_auth_fields = {}

        # Enable user account
        if db_adapter.UserProfileClass:
            if hasattr(db_adapter.UserProfileClass, 'active'):
                user_auth_fields['active'] = True
            elif hasattr(db_adapter.UserProfileClass, 'is_enabled'):
                user_auth_fields['is_enabled'] = True
            else:
                user_auth_fields['is_active'] = True
        else:
            if hasattr(db_adapter.UserClass, 'active'):
                user_fields['active'] = True
            elif hasattr(db_adapter.UserClass, 'is_enabled'):
                user_fields['is_enabled'] = True
            else:
                user_fields['is_active'] = True

        # For all form fields
        for field_name, field_value in register_form.data.items():
            # Hash password field
            if field_name=='password':
                hashed_password = user_manager.hash_password(field_value)
                if db_adapter.UserAuthClass:
                    user_auth_fields['password'] = hashed_password
                else:
                    user_fields['password'] = hashed_password
            # Store corresponding Form fields into the User object and/or UserProfile object
            else:
                if field_name in user_class_fields:
                    user_fields[field_name] = field_value
                if db_adapter.UserEmailClass:
                    if field_name in user_email_class_fields:
                        user_email_fields[field_name] = field_value
                if db_adapter.UserAuthClass:
                    if field_name in user_auth_class_fields:
                        user_auth_fields[field_name] = field_value

        # Add User record using named arguments 'user_fields'
        user = db_adapter.add_object(User, **user_fields)
        if db_adapter.UserProfileClass:
            user_profile = user

        # Add UserEmail record using named arguments 'user_email_fields'
        if db_adapter.UserEmailClass:
            user_email = db_adapter.add_object(UserEmail,
                    user=user,
                    is_primary=True,
                    **user_email_fields)
        else:
            user_email = None

        # Add UserAuth record using named arguments 'user_auth_fields'
        if db_adapter.UserAuthClass:
            user_auth = db_adapter.add_object(UserAuth, **user_auth_fields)
            if db_adapter.UserProfileClass:
                user = user_auth
            else:
                user.user_auth = user_auth

        db_adapter.commit()

        # Send 'registered' email and delete new User object if send fails
        if user_manager.send_registered_email:
            try:
                # Send 'registered' email
                _send_registered_email(user, user_email)
            except Exception as e:
                # delete new User object if send  fails
                db_adapter.delete_object(user)
                db_adapter.commit()
                raise e

        # Send user_registered signal
        signals.user_registered.send(current_app._get_current_object(), user=user)

        # Redirect if USER_ENABLE_CONFIRM_EMAIL is set
        if user_manager.enable_confirm_email:
            next = request.args.get('next', _endpoint_url(user_manager.after_register_endpoint))
            return redirect(next)

        # Auto-login after register or redirect to login page
        next = request.args.get('next', _endpoint_url(user_manager.after_confirm_endpoint))
        if user_manager.auto_login_after_register:
            return _do_login_user(user, reg_next)                     # auto-login
        else:
            return redirect(url_for('user.login')+'?next='+reg_next)  # redirect to login page

    # Process GET or invalid POST
    return render_template(user_manager.register_template,
            form=register_form,
            login_form=login_form,
            register_form=register_form)

def resend_confirm_email():
    """Prompt for email and re-send email conformation email."""
    user_manager =  current_app.user_manager
    db_adapter = user_manager.db_adapter

    # Initialize form
    form = user_manager.resend_confirm_email_form(request.form)

    # Process valid POST
    if request.method=='POST' and form.validate():
        email = form.email.data

        # Find user by email
        user, user_email = user_manager.find_user_by_email(email)
        if user:
            _send_confirm_email(user, user_email)

        # Redirect to the login page
        return redirect(_endpoint_url(user_manager.after_resend_confirm_email_endpoint))

    # Process GET or invalid POST
    return render_template(user_manager.resend_confirm_email_template, form=form)


def reset_password(token):
    """ Verify the password reset token, Prompt for new password, and set the user's password."""
    # Verify token
    user_manager = current_app.user_manager
    db_adapter = user_manager.db_adapter

    is_valid, has_expired, user_id = user_manager.verify_token(
            token,
            user_manager.reset_password_expiration)

    if has_expired:
        flash(_('Your reset password token has expired.'), 'error')
        return redirect(url_for('user.login'))

    if not is_valid:
        flash(_('Your reset password token is invalid.'), 'error')
        return redirect(url_for('user.login'))

    user = user_manager.get_user_by_id(user_id)
    if user:
        # Avoid re-using old tokens
        if hasattr(user, 'reset_password_token'):
            verified = user.reset_password_token == token
        else:
            verified = True
    if not user or not verified:
        flash(_('Your reset password token is invalid.'), 'error')
        return redirect(_endpoint_url(user_manager.login_endpoint))

    # Initialize form
    form = user_manager.reset_password_form(request.form)

    # Process valid POST
    if request.method=='POST' and form.validate():
        # Invalidate the token by clearing the stored token
        if hasattr(user, 'reset_password_token'):
            db_adapter.update_object(user, reset_password_token='')

        # Change password
        hashed_password = user_manager.hash_password(form.new_password.data)
        user_auth = user.user_auth if db_adapter.UserAuthClass and hasattr(user, 'user_auth') else user
        db_adapter.update_object(user_auth, password=hashed_password)
        db_adapter.commit()

        # Send 'password_changed' email
        if user_manager.enable_email and user_manager.send_password_changed_email:
            emails.send_password_changed_email(user)

        # Prepare one-time system message
        flash(_("Your password has been reset successfully. Please sign in with your new password"), 'success')

        # Auto-login after reset password or redirect to login page
        next = request.args.get('next', _endpoint_url(user_manager.after_reset_password_endpoint))
        if user_manager.auto_login_after_reset_password:
            return _do_login_user(user, next)                       # auto-login
        else:
            return redirect(url_for('user.login')+'?next='+next)    # redirect to login page

    # Process GET or invalid POST
    return render_template(user_manager.reset_password_template, form=form)


def unconfirmed():
    """ Prepare a Flash message and redirect to USER_UNCONFIRMED_URL"""
    # Prepare Flash message
    url = request.script_root + request.path
    flash(_("You must confirm your email to access '%(url)s'.", url=url), 'error')

    # Redirect to USER_UNCONFIRMED_EMAIL_ENDPOINT
    user_manager = current_app.user_manager
    return redirect(_endpoint_url(user_manager.unconfirmed_email_endpoint))


def unauthenticated():
    """ Prepare a Flash message and redirect to USER_UNAUTHENTICATED_ENDPOINT"""
    # Prepare Flash message
    url = request.url
    flash(_("You must be signed in to access '%(url)s'.", url=url), 'error')

    # quote the fully qualified url
    quoted_url = quote(url)

    # Redirect to USER_UNAUTHENTICATED_ENDPOINT
    user_manager = current_app.user_manager
    return redirect(_endpoint_url(user_manager.unauthenticated_endpoint)+'?next='+ quoted_url)


def unauthorized():
    """ Prepare a Flash message and redirect to USER_UNAUTHORIZED_ENDPOINT"""
    # Prepare Flash message
    url = request.script_root + request.path
    flash(_("You do not have permission to access '%(url)s'.", url=url), 'error')

    # Redirect to USER_UNAUTHORIZED_ENDPOINT
    user_manager = current_app.user_manager
    return redirect(_endpoint_url(user_manager.unauthorized_endpoint))

def _endpoint_url(endpoint):
    url = '/'
    if endpoint:
        url = url_for(endpoint)
    return url