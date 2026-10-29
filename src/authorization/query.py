from sqlalchemy.sql import text
from src.config.db import CONSOLE_DB


def get_admin_login_query():
    return text(f"""
        SELECT * FROM {CONSOLE_DB}.con_user_master cum
        WHERE con_user_login_email_id = :username 
          AND cum.con_user_type = 0
          AND cum.con_org_id IS NULL
    """)

def get_company_admin_login_query():
    return text(f"""
        SELECT cum.*, com.con_org_id AS matched_org_id
        FROM {CONSOLE_DB}.con_user_master cum
        INNER JOIN {CONSOLE_DB}.con_org_master com ON com.con_org_id = cum.con_org_id
        WHERE cum.con_user_login_email_id = :username 
          AND cum.con_user_type = 1
          AND LOWER(TRIM(com.con_org_shortname)) = LOWER(TRIM(:subdomain))
    """)


def get_org_id_by_subdomain_query():
    return text(f"""
        SELECT con_org_id
        FROM {CONSOLE_DB}.con_org_master
        WHERE LOWER(TRIM(con_org_shortname)) = LOWER(TRIM(:subdomain))
        LIMIT 1
    """)


def validate_subdomain_query():
    return text(f"""
        SELECT COUNT(*) as cnt
        FROM {CONSOLE_DB}.con_org_master
        WHERE LOWER(TRIM(con_org_shortname)) = LOWER(TRIM(:subdomain))
          AND active = 1
        LIMIT 1
    """)