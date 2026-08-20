import os
from datetime import date, timedelta
from app import app, db, User, Medicine, Batch
from werkzeug.security import generate_password_hash

app.config["WTF_CSRF_ENABLED"] = False
c = app.test_client()

# login checks
r = c.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=True)
assert "غير صحيحة" in r.get_data(as_text=True), "bad-login message missing"
r = c.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
assert "لوحة التحكم" in r.get_data(as_text=True), "admin login failed"

for url in ["/dashboard", "/medicines", "/medicines/add", "/batches/add", "/reports/inventory",
            "/reports/inventory/print", "/users", "/users/add", "/alerts"]:
    assert c.get(url).status_code == 200, url

# add medicines
c.post("/medicines/add", data={"name": "dolipran 500", "min_stock": 50}, follow_redirects=True)
c.post("/medicines/add", data={"name": "هيمانات", "min_stock": 20}, follow_redirects=True)
c.post("/medicines/add", data={"name": "دواء X", "min_stock": 20}, follow_redirects=True)
r = c.post("/medicines/add", data={"name": "", "min_stock": 5}, follow_redirects=True)
assert "اسم الدواء مطلوب" in r.get_data(as_text=True)

with app.app_context():
    m1 = Medicine.query.filter_by(name="dolipran 500").first()
    m2 = Medicine.query.filter_by(name="هيمانات").first()
    mx = Medicine.query.filter_by(name="دواء X").first()
    ids = (m1.id, m2.id, mx.id)

d1 = (date.today() + timedelta(days=90)).isoformat()
d2 = (date.today() + timedelta(days=400)).isoformat()
c.post("/batches/add", data={"medicine_id": ids[0], "quantity": 9, "expire_date": d1}, follow_redirects=True)
c.post("/batches/add", data={"medicine_id": ids[0], "quantity": 40, "expire_date": d2}, follow_redirects=True)
c.post("/batches/add", data={"medicine_id": ids[1], "quantity": 30, "expire_date": d1}, follow_redirects=True)
r = c.post("/batches/add", data={"medicine_id": ids[0], "quantity": -5, "expire_date": d1}, follow_redirects=True)
assert "أكبر من صفر" in r.get_data(as_text=True)

with app.app_context():
    assert db.session.get(Medicine, ids[0]).total_quantity == 49

# dispense 10 -> FIFO
r = c.post("/medicines/%d/dispense" % ids[0], data={"quantity": 10}, follow_redirects=True)
with app.app_context():
    m = db.session.get(Medicine, ids[0])
    assert m.total_quantity == 39, m.total_quantity
    qs = sorted([b.quantity for b in m.batches])
    assert qs == [0, 39], qs
r = c.post("/medicines/%d/dispense" % ids[0], data={"quantity": 9999}, follow_redirects=True)
assert "أكبر من المخزون" in r.get_data(as_text=True)

# report: no zero rows / zero batches
r = c.get("/reports/inventory/print").get_data(as_text=True)
assert "dolipran 500" in r and "هيمانات" in r and "دواء X" not in r
assert "القائمة الجانبية" not in r and "إدارة المستخدمين" not in r and "تسجيل الخروج" not in r
assert r.count("dolipran 500") == 2, r.count("dolipran 500")  # one stock row + one active batch row

# users management
r = c.post("/users/add", data={"username": "ali", "password": "1234", "confirm": "1234", "role": "user"}, follow_redirects=True)
assert "بنجاح" in r.get_data(as_text=True)
r = c.post("/users/add", data={"username": "ali", "password": "1234", "confirm": "1234", "role": "user"}, follow_redirects=True)
assert "مستخدم مسبقاً" in r.get_data(as_text=True)
with app.app_context():
    admin_id = User.query.filter_by(username="admin").first().id
r = c.post("/users/%d/delete" % admin_id, follow_redirects=True)
assert "لا يمكن حذف المدير الرئيسي" in r.get_data(as_text=True)
c.get("/logout")

# normal user restrictions
c2 = app.test_client()
c2.post("/login", data={"username": "ali", "password": "1234"}, follow_redirects=True)
for url in ["/reports/inventory", "/reports/inventory/print", "/users", "/users/add"]:
    r = c2.get(url, follow_redirects=True)
    assert "ليس لديك صلاحية للوصول إلى هذه الصفحة." in r.get_data(as_text=True), url
r = c2.post("/users/add", data={"username": "z", "password": "1234", "confirm": "1234", "role": "admin"}, follow_redirects=True)
assert "ليس لديك صلاحية" in r.get_data(as_text=True)
r = c2.post("/medicines/%d/delete" % ids[2], follow_redirects=True)
assert "ليس لديك صلاحية" in r.get_data(as_text=True)
assert c2.get("/medicines").status_code == 200
assert c2.get("/dashboard").status_code == 200
assert "إدارة المستخدمين" not in c2.get("/dashboard").get_data(as_text=True)

# anonymous redirect
c3 = app.test_client()
r = c3.get("/dashboard", follow_redirects=True)
assert "تسجيل الدخول" in r.get_data(as_text=True)

# all url_for endpoints exist
print("ROUTES:", sorted(r.rule for r in app.url_map.iter_rules()))
print("ALL TESTS PASSED")

