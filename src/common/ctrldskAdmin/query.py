from sqlalchemy.sql import text
from src.config.db import CONSOLE_DB
from sqlalchemy.sql import bindparam
from sqlalchemy.sql.elements import TextClause

def get_portal_menu_details(search: str = None):
    print(f" query get_portal_menu_details called with search: {search} ")
    sql = f"""
        SELECT 
            menu.menu_id,
            menu.menu_name,
            menu.menu_parent_id AS parent_id,
            menu.module_id,
            cmm.con_module_name,
            CASE 
                WHEN menu.menu_parent_id > 0 THEN (SELECT parent.menu_name 
                                                  FROM {CONSOLE_DB}.portal_menu_mst parent 
                                                  WHERE parent.menu_id = menu.menu_parent_id)
                ELSE ''
            END AS parent_name,
            menu.active,
            menu.menu_path,
            menu.menu_icon,
            menu.module_id
        FROM {CONSOLE_DB}.portal_menu_mst menu
        LEFT JOIN {CONSOLE_DB}.con_module_masters cmm ON menu.module_id = cmm.con_module_id 
        WHERE cmm.active = 1
    """

    # Add search filter if search is provided
    if search:
        sql += " AND menu.menu_name LIKE :search "

    sql += " ORDER BY menu.order_by, menu.menu_id LIMIT :limit OFFSET :offset "

    return text(sql)


def get_active_orgs_query():
    sql = """
        SELECT con_org_id, con_org_name, con_org_shortname
        FROM con_org_master
        WHERE active = 1
        ORDER BY con_org_name
    """
    return text(sql)


def get_tenant_counts_query(db_name: str):
    # db_name MUST already be validated against information_schema.schemata by
    # the caller — it is interpolated, never bound.
    # co_mst has no active flag, so companies = all companies in the tenant.
    sql = f"""
        SELECT
            (SELECT COUNT(*) FROM `{db_name}`.co_mst) AS companies,
            (SELECT COUNT(*) FROM `{db_name}`.user_mst WHERE active = 1) AS active_users
    """
    return text(sql)


def get_portal_menu_details_count(search: str = None):
    sql = f"""
        SELECT COUNT(*) AS total
        FROM {CONSOLE_DB}.portal_menu_mst menu
        LEFT JOIN {CONSOLE_DB}.con_module_masters cmm ON menu.module_id = cmm.con_module_id
        WHERE cmm.active = 1
    """
    if search:
        sql += " AND menu.menu_name LIKE :search "
    return text(sql)





def portal_parentmenudetails():
    #print(f" query portal_parentmenudetails called with moduleId: {moduleId} ")
    sql = f"""SELECT 
                menu.menu_id,
                menu.menu_name,
                menu.module_id
            FROM {CONSOLE_DB}.portal_menu_mst menu
            WHERE menu.menu_parent_id = 0 AND menu.active = 1"""
    sql += " ORDER BY module_id, menu.order_by"""
    #print(f"SQL Query: {sql}{moduleId}")
    
    return text(sql)





def portalmodulename():
    print(f" query portalmodulename called ")
    sql=f"""SELECT 
                menu.con_module_id,
                menu.con_module_name
                FROM {CONSOLE_DB}.con_module_masters menu
                where  menu.active = 1
                order by menu.con_module_name            
"""
    return text(sql)



def portalmenutypedetails():
    print(f" query portalmenutypedetails called ")
    sql=f"""SELECT * 
                FROM {CONSOLE_DB}.menu_type_mst
            
"""
    return text(sql)

def get_portalmenuname_by_name(name: str = None, menu_id: int = None):
    print(f" query get_portalmenuname_by_name called with name: {name} ")
    if menu_id == 0:
        sql = f"""SELECT count(*) as count 
                FROM {CONSOLE_DB}.portal_menu_mst menu
                where menu.menu_name = :name            
        """
    else:
        sql = f"""SELECT count(*) as count 
                FROM {CONSOLE_DB}.portal_menu_mst menu
                where menu.menu_name = :name and menu.menu_id != :menu_id            
        """
    return text(sql)



def get_menu_by_id_query(co_id: int):
    sql = f"""select menu_id,menu_name,menu_path,active,menu_parent_id,menu_type_id,
    menu_icon,module_id,order_by
    from {CONSOLE_DB}.portal_menu_mst
    where menu_id = :co_id;"""
    query = text(sql)
    return query



def orgmodulename(search: str = None, paged: bool = False):
    print(f" query orgmodulename called ")
    # :search / :limit / :offset are only bound in when asked for — the
    # unpaged caller (orgmodulemapdetails1) supplies no params at all.
    sql = f"""SELECT
            com.con_org_id AS org_id,
            com.con_org_name AS org_name,
            GROUP_CONCAT(cmm.con_module_name SEPARATOR '/') AS module_selected
            FROM
            {CONSOLE_DB}.con_org_master com
            LEFT JOIN JSON_TABLE(
            com.con_modules_selected,
            '$[*]' COLUMNS (
            module_id VARCHAR(10) PATH '$'
            )
        ) AS jt ON TRUE
        LEFT JOIN con_module_masters cmm
        ON cmm.con_module_id = jt.module_id
        WHERE
        com.con_org_master_status = 3
"""
    if search:
        sql += " AND com.con_org_name LIKE :search "

    sql += """
        GROUP BY
        com.con_org_id, com.con_org_name
        ORDER BY com.con_org_id
"""
    if paged:
        sql += " LIMIT :limit OFFSET :offset "

    return text(sql)


def orgmodulename_count(search: str = None):
    # One row per org after the GROUP BY, so counting the base table matches.
    sql = f"""
        SELECT COUNT(*) AS total
        FROM {CONSOLE_DB}.con_org_master com
        WHERE com.con_org_master_status = 3
    """
    if search:
        sql += " AND com.con_org_name LIKE :search "
    return text(sql)

 

# text(f"""
#             SELECT 
#                 menu.menu_id,
#                 menu.menu_name,
#                 menu.menu_parent_id AS parent_id,
#                 CASE 
#                     WHEN menu.menu_parent_id > 0 THEN (SELECT parent.menu_name 
#                                                       FROM {dbname}.portal_menu_mst parent 
#                                                       WHERE parent.menu_id = menu.menu_parent_id)
#                     ELSE ''
#                 END AS parent_name,
#                 menu.active,
#                 menu.menu_path,
#                 menu.menu_icon,
#                 menu.module_id
#             FROM {dbname}.portal_menu_mst menu
#         """)
