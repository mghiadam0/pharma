import os
import sqlite3
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, flash, redirect, render_template, request, session,
                   url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)
DB_PATH = os.path.join(INSTANCE_DIR, "medical_inventory.db")

NEAR_EXPIRY_DAYS = 180

app = Flask(__name__)
app.config["SECRET_KEY"] = "medical-inventory-secret-key"

# ================================================================
# 🔥 التغيير الأساسي: دعم SQLite محلياً و PostgreSQL (Supabase)
# ================================================================

# جلب رابط قاعدة البيانات من متغير البيئة (إن وجد)
database_url = os.environ.get('DATABASE_URL')

if database_url:
    # تصحيح الرابط: postgres:// → postgresql://
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    
    # إضافة sslmode=require إن لم يكن موجوداً
    if 'sslmode' not in database_url:
        if '?' in database_url:
            database_url += '&sslmode=require'
        else:
            database_url += '?sslmode=require'
    
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    print("✅ باستخدام قاعدة بيانات PostgreSQL (Supabase)")
else:
    # استخدام SQLite محلياً (للتطوير على Termux)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + DB_PATH
    print("✅ باستخدام قاعدة بيانات SQLite محلية")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ================================================================
# إعدادات إضافية لتحسين أداء الاتصال بـ PostgreSQL
# ================================================================

if database_url:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': 5,          # عدد الاتصالات النشطة
        'max_overflow': 10,      # اتصالات إضافية عند الحاجة
        'pool_timeout': 30,      # وقت انتظار الاتصال
        'pool_recycle': 1800,    # إعادة تعيين الاتصال كل 30 دقيقة
    }

# ================================================================

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "الرجاء تسجيل الدخول أولاً."
login_manager.login_message_category = "warning"


# ------------------------------------------------------------------
# النماذج (لم تتغير)
# ------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def role_label(self):
        return "مدير" if self.is_admin else "مستخدم"


class Medicine(db.Model):
    __tablename__ = "medicine"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    min_stock = db.Column(db.Integer, nullable=False, default=0)

    batches = db.relationship(
        "Batch",
        backref="medicine",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Batch.expire_date",
    )

    @property
    def total_quantity(self):
        return sum(b.quantity for b in self.batches)

    @property
    def active_batches(self):
        return [b for b in self.batches if b.quantity > 0]

    @property
    def is_low(self):
        return self.total_quantity <= self.min_stock

    @property
    def nearest_expire(self):
        dates = [b.expire_date for b in self.active_batches if b.expire_date]
        return min(dates) if dates else None


class Batch(db.Model):
    __tablename__ = "batch"
    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey("medicine.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    expire_date = db.Column(db.Date, nullable=True)

    @property
    def is_expired(self):
        return bool(self.expire_date and self.expire_date < date.today())

    @property
    def is_near_expiry(self):
        if not self.expire_date or self.is_expired:
            return False
        return self.expire_date <= date.today() + timedelta(days=NEAR_EXPIRY_DAYS)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ------------------------------------------------------------------
# الصلاحيات (لم تتغير)
# ------------------------------------------------------------------
def admin_required(view_function):
    @wraps(view_function)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role != "admin":
            flash("ليس لديك صلاحية للوصول إلى هذه الصفحة.", "danger")
            return redirect(url_for("dashboard"))
        return view_function(*args, **kwargs)

    return wrapped_view


# ------------------------------------------------------------------
# أدوات مساعدة (لم تتغير)
# ------------------------------------------------------------------
def parse_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_date(value):
    try:
        y, m, d = str(value).strip().split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def get_alerts():
    alerts = []
    today = date.today()
    limit = today + timedelta(days=NEAR_EXPIRY_DAYS)
    for med in Medicine.query.order_by(Medicine.name).all():
        total = med.total_quantity
        if total <= med.min_stock:
            alerts.append({
                "type": "مخزون منخفض",
                "category": "warning",
                "text": "الدواء «%s» مخزونه %d والحد الأدنى %d." % (med.name, total, med.min_stock),
            })
        for b in med.active_batches:
            if not b.expire_date:
                continue
            if b.expire_date < today:
                alerts.append({
                    "type": "منتهي الصلاحية",
                    "category": "danger",
                    "text": "شحنة من «%s» بكمية %d انتهت صلاحيتها في %s." % (
                        med.name, b.quantity, b.expire_date.strftime("%d-%m-%Y")),
                })
            elif b.expire_date <= limit:
                alerts.append({
                    "type": "قرب انتهاء الصلاحية",
                    "category": "info",
                    "text": "شحنة من «%s» بكمية %d تنتهي في %s." % (
                        med.name, b.quantity, b.expire_date.strftime("%d-%m-%Y")),
                })
    return alerts


@app.context_processor
def inject_globals():
    unread = 0
    if current_user.is_authenticated:
        if not session.get("alerts_read"):
            unread = len(get_alerts())
    return {
        "alerts_count": unread,
        "today_str": date.today().strftime("%d-%m-%Y"),
        "near_days": NEAR_EXPIRY_DAYS,
    }


# ------------------------------------------------------------------
# المصادقة (لم تتغير)
# ------------------------------------------------------------------
@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            session["alerts_read"] = False
            return redirect(url_for("dashboard"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("alerts_read", None)
    flash("تم تسجيل الخروج بنجاح.", "success")
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# لوحة التحكم (لم تتغير)
# ------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    meds = Medicine.query.order_by(Medicine.name).all()
    today = date.today()
    limit = today + timedelta(days=NEAR_EXPIRY_DAYS)
    batches = Batch.query.filter(Batch.quantity > 0).all()
    stats = {
        "medicines": len(meds),
        "quantities": sum(m.total_quantity for m in meds),
        "low": len([m for m in meds if m.is_low]),
        "expired": len([b for b in batches if b.expire_date and b.expire_date < today]),
        "near": len([b for b in batches if b.expire_date and today <= b.expire_date <= limit]),
        "batches": len(batches),
    }
    low_list = [m for m in meds if m.is_low][:8]
    return render_template("dashboard.html", stats=stats, low_list=low_list)


@app.route("/alerts")
@login_required
def alerts():
    data = get_alerts()
    session["alerts_read"] = True
    return render_template("alerts.html", alerts=data)


# ------------------------------------------------------------------
# الأدوية (لم تتغير)
# ------------------------------------------------------------------
@app.route("/medicines")
@login_required
def medicines():
    q = (request.args.get("q") or "").strip()
    query = Medicine.query
    if q:
        query = query.filter(Medicine.name.like("%" + q + "%"))
    return render_template("medicines.html", medicines=query.order_by(Medicine.name).all(), q=q)


@app.route("/medicines/add", methods=["GET", "POST"])
@login_required
def add_medicine():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        min_stock = parse_int(request.form.get("min_stock"), 0)
        if not name:
            flash("اسم الدواء مطلوب.", "danger")
        elif min_stock < 0:
            flash("الحد الأدنى للمخزون لا يمكن أن يكون سالباً.", "danger")
        elif Medicine.query.filter_by(name=name).first():
            flash("يوجد دواء بنفس الاسم.", "danger")
        else:
            db.session.add(Medicine(name=name, min_stock=min_stock))
            db.session.commit()
            session["alerts_read"] = False
            flash("تمت إضافة الدواء بنجاح.", "success")
            return redirect(url_for("medicines"))
    return render_template("add_medicine.html")


@app.route("/medicines/<int:medicine_id>/edit", methods=["GET", "POST"])
@login_required
def edit_medicine(medicine_id):
    med = db.get_or_404(Medicine, medicine_id)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        min_stock = parse_int(request.form.get("min_stock"), 0)
        exists = Medicine.query.filter(Medicine.name == name, Medicine.id != med.id).first()
        if not name:
            flash("اسم الدواء مطلوب.", "danger")
        elif min_stock < 0:
            flash("الحد الأدنى للمخزون لا يمكن أن يكون سالباً.", "danger")
        elif exists:
            flash("يوجد دواء بنفس الاسم.", "danger")
        else:
            med.name = name
            med.min_stock = min_stock
            db.session.commit()
            session["alerts_read"] = False
            flash("تم تعديل الدواء بنجاح.", "success")
            return redirect(url_for("medicines"))
    return render_template("edit_medicine.html", med=med)


@app.route("/medicines/<int:medicine_id>/delete", methods=["POST"])
@admin_required
def delete_medicine(medicine_id):
    med = db.get_or_404(Medicine, medicine_id)
    db.session.delete(med)
    db.session.commit()
    flash("تم حذف الدواء وجميع شحناته.", "success")
    return redirect(url_for("medicines"))


@app.route("/medicines/<int:medicine_id>/batches")
@login_required
def medicine_batches(medicine_id):
    med = db.get_or_404(Medicine, medicine_id)
    return render_template("batches.html", med=med)


# ------------------------------------------------------------------
# الشحنات - إدخال (لم تتغير)
# ------------------------------------------------------------------
@app.route("/batches/add", methods=["GET", "POST"])
@login_required
def add_batch():
    meds = Medicine.query.order_by(Medicine.name).all()
    selected = parse_int(request.args.get("medicine_id"), 0)
    if request.method == "POST":
        medicine_id = parse_int(request.form.get("medicine_id"), 0)
        quantity = parse_int(request.form.get("quantity"), -1)
        expire_date = parse_date(request.form.get("expire_date"))
        med = db.session.get(Medicine, medicine_id)
        if not med:
            flash("الرجاء اختيار دواء صحيح.", "danger")
        elif quantity <= 0:
            flash("الكمية يجب أن تكون رقماً أكبر من صفر.", "danger")
        elif not expire_date:
            flash("تاريخ انتهاء الصلاحية مطلوب.", "danger")
        else:
            db.session.add(Batch(medicine_id=med.id, quantity=quantity, expire_date=expire_date))
            db.session.commit()
            session["alerts_read"] = False
            flash("تم إدخال الكمية وتحديث المخزون.", "success")
            return redirect(url_for("medicine_batches", medicine_id=med.id))
        selected = medicine_id
    return render_template("add_batch.html", medicines=meds, selected=selected)


@app.route("/batches/<int:batch_id>/edit", methods=["GET", "POST"])
@login_required
def edit_batch(batch_id):
    batch = db.get_or_404(Batch, batch_id)
    if request.method == "POST":
        quantity = parse_int(request.form.get("quantity"), -1)
        expire_date = parse_date(request.form.get("expire_date"))
        if quantity < 0:
            flash("الكمية لا يمكن أن تكون سالبة.", "danger")
        elif not expire_date:
            flash("تاريخ انتهاء الصلاحية مطلوب.", "danger")
        else:
            batch.quantity = quantity
            batch.expire_date = expire_date
            db.session.commit()
            session["alerts_read"] = False
            flash("تم تعديل الشحنة بنجاح.", "success")
            return redirect(url_for("medicine_batches", medicine_id=batch.medicine_id))
    return render_template("edit_batch.html", batch=batch)


@app.route("/batches/<int:batch_id>/delete", methods=["POST"])
@admin_required
def delete_batch(batch_id):
    batch = db.get_or_404(Batch, batch_id)
    medicine_id = batch.medicine_id
    db.session.delete(batch)
    db.session.commit()
    flash("تم حذف الشحنة.", "success")
    return redirect(url_for("medicine_batches", medicine_id=medicine_id))


# ------------------------------------------------------------------
# إخراج كمية (FIFO) (لم تتغير)
# ------------------------------------------------------------------
@app.route("/medicines/<int:medicine_id>/dispense", methods=["GET", "POST"])
@login_required
def dispense(medicine_id):
    med = db.get_or_404(Medicine, medicine_id)
    if request.method == "POST":
        quantity = parse_int(request.form.get("quantity"), 0)
        available = med.total_quantity
        if quantity <= 0:
            flash("حدد كمية صحيحة أكبر من صفر.", "danger")
        elif quantity > available:
            flash("الكمية المطلوبة أكبر من المخزون المتوفر (%d)." % available, "danger")
        else:
            remaining = quantity
            batches = sorted(
                med.active_batches,
                key=lambda b: (b.expire_date or date.max, b.id),
            )
            for b in batches:
                if remaining <= 0:
                    break
                taken = min(b.quantity, remaining)
                b.quantity -= taken
                remaining -= taken
            db.session.commit()
            session["alerts_read"] = False
            flash("تم إخراج %d من «%s». الكمية الحالية: %d" % (quantity, med.name, med.total_quantity), "success")
            return redirect(url_for("medicines"))
    return render_template("dispense.html", med=med)


# ------------------------------------------------------------------
# التقارير - للمدير فقط (لم تتغير)
# ------------------------------------------------------------------
def build_report():
    today = date.today()
    limit = today + timedelta(days=NEAR_EXPIRY_DAYS)
    meds = Medicine.query.order_by(Medicine.name).all()
    rows = [m for m in meds if m.total_quantity > 0]
    batch_rows = (
        Batch.query.filter(Batch.quantity > 0)
        .join(Medicine)
        .order_by(Medicine.name, Batch.expire_date)
        .all()
    )
    return {
        "date": today.strftime("%d-%m-%Y"),
        "rows": rows,
        "batch_rows": batch_rows,
        "total_medicines": len(meds),
        "total_quantity": sum(m.total_quantity for m in meds),
        "low": [m for m in meds if m.is_low],
        "expired": [b for b in batch_rows if b.expire_date and b.expire_date < today],
        "near": [b for b in batch_rows if b.expire_date and today <= b.expire_date <= limit],
    }


@app.route("/reports/inventory")
@admin_required
def inventory_report():
    return render_template("inventory_report.html", r=build_report())


@app.route("/reports/inventory/print")
@admin_required
def inventory_report_print():
    return render_template("report_print.html", r=build_report())


# ------------------------------------------------------------------
# إدارة المستخدمين - للمدير فقط (لم تتغير)
# ------------------------------------------------------------------
@app.route("/users")
@admin_required
def users():
    return render_template("users.html", users=User.query.order_by(User.username).all())


@app.route("/users/add", methods=["GET", "POST"])
@admin_required
def add_user():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        role = request.form.get("role") if request.form.get("role") in ("admin", "user") else "user"
        if not username:
            flash("اسم المستخدم مطلوب.", "danger")
        elif len(password) < 4:
            flash("كلمة المرور يجب أن تكون 4 أحرف على الأقل.", "danger")
        elif password != confirm:
            flash("كلمتا المرور غير متطابقتين.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("اسم المستخدم مستخدم مسبقاً.", "danger")
        else:
            db.session.add(User(username=username,
                                password=generate_password_hash(password),
                                role=role))
            db.session.commit()
            flash("تمت إضافة المستخدم بنجاح.", "success")
            return redirect(url_for("users"))
    return render_template("add_user.html")


@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    user = db.get_or_404(User, user_id)
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        role = request.form.get("role") if request.form.get("role") in ("admin", "user") else "user"
        exists = User.query.filter(User.username == username, User.id != user.id).first()
        if not username:
            flash("اسم المستخدم مطلوب.", "danger")
        elif exists:
            flash("اسم المستخدم مستخدم مسبقاً.", "danger")
        elif password and password != confirm:
            flash("كلمتا المرور غير متطابقتين.", "danger")
        elif password and len(password) < 4:
            flash("كلمة المرور يجب أن تكون 4 أحرف على الأقل.", "danger")
        else:
            if user.username == "admin" and role != "admin":
                flash("لا يمكن تغيير صلاحية المدير الرئيسي.", "warning")
            else:
                user.role = role
            user.username = username
            if password:
                user.password = generate_password_hash(password)
            db.session.commit()
            flash("تم تعديل المستخدم بنجاح.", "success")
            return redirect(url_for("users"))
    return render_template("edit_user.html", user=user)


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = db.get_or_404(User, user_id)
    if user.username == "admin":
        flash("لا يمكن حذف المدير الرئيسي.", "danger")
    elif user.id == current_user.id:
        flash("لا يمكنك حذف حسابك الحالي.", "danger")
    else:
        db.session.delete(user)
        db.session.commit()
        flash("تم حذف المستخدم.", "success")
    return redirect(url_for("users"))


# ------------------------------------------------------------------
# 🔥 تغيير مهم: تهيئة وترقية قاعدة البيانات
# ------------------------------------------------------------------
def upgrade_database():
    """إضافة عمود role للقواعد القديمة بدون فقدان أي بيانات."""
    if not os.path.exists(DB_PATH):
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user'")
    if cur.fetchone():
        cols = [row[1] for row in cur.execute("PRAGMA table_info(user)")]
        if "role" not in cols:
            cur.execute("ALTER TABLE user ADD COLUMN role VARCHAR(20) DEFAULT 'user'")
            cur.execute("UPDATE user SET role='user' WHERE role IS NULL OR role=''")
            cur.execute("UPDATE user SET role='admin' WHERE username='admin'")
            con.commit()
    con.close()


def init_db():
    """تهيئة قاعدة البيانات وإنشاء المستخدم admin إن لم يكن موجوداً."""
    with app.app_context():
        # إنشاء الجداول (تعمل مع SQLite و PostgreSQL)
        db.create_all()
        
        # التحقق من وجود مستخدم admin
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            admin = User(
                username="admin",
                password=generate_password_hash("admin123"),
                role="admin"
            )
            db.session.add(admin)
            db.session.commit()
            print("✅ تم إنشاء مستخدم admin")
        elif admin.role != "admin":
            admin.role = "admin"
            db.session.commit()
            print("✅ تم تحديث صلاحية admin")


# ================================================================
# 🔥 تشغيل التطبيق مع تهيئة القاعدة
# ================================================================

# ترقية قاعدة SQLite القديمة (إن وجدت)
upgrade_database()

# تهيئة قاعدة البيانات (إنشاء الجداول والمستخدم admin)
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
