from src.hrms.payRegister import ecr_line


def test_contributing_line():
    # EPS defaults to 8.33% of PF wages; ER diff = EE - EPS.
    assert ecr_line("100200300400", "ram  kumar ", 15500.4, 15000, 1800, None, 4) == (
        "100200300400#~#RAM KUMAR#~#15500#~#15000#~#15000#~#15000#~#1800#~#1250#~#550#~#4#~#0"
    )


def test_non_contributing_line():
    assert ecr_line("1001", "A#B", ncp=31) == "1001#~#AB#~#0#~#0#~#0#~#0#~#0#~#0#~#0#~#31#~#0"
