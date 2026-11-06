-- ============================================================================
--  GRAIN INVENTORY SYSTEM  -  Complete database schema + starter data
--  Requires MySQL 8.0.16+ (CHECK constraints are enforced from that version)
--
--  !!! WARNING !!!
--  The DROP block below wipes all existing tables so the schema can be rebuilt.
--  Run this file ONCE for a fresh install. After that, delete/comment out the
--  "DROP" block if you ever re-run the file, or you will lose your data.
--
--  Users (owner / cashier1) are created automatically by the backend on its
--  first start, with securely hashed passwords. See README.md.
-- ============================================================================

CREATE DATABASE IF NOT EXISTS grain_inventory
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE grain_inventory;

-- ---------------------------------------------------------------- DROP BLOCK
SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS Audit_log;
DROP TABLE IF EXISTS Stock_log;
DROP TABLE IF EXISTS Order_item;
DROP TABLE IF EXISTS Orders;
DROP TABLE IF EXISTS Customer;
DROP TABLE IF EXISTS Offer;
DROP TABLE IF EXISTS Stock;
DROP TABLE IF EXISTS Product;
DROP TABLE IF EXISTS Variety;
DROP TABLE IF EXISTS Category;
DROP TABLE IF EXISTS System_Users;
SET FOREIGN_KEY_CHECKS = 1;
-- ------------------------------------------------------------ END DROP BLOCK

-- 1. Staff accounts ----------------------------------------------------------
CREATE TABLE System_Users (
    user_id       INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL UNIQUE,
    full_name     VARCHAR(100) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role          ENUM('owner', 'cashier') NOT NULL DEFAULT 'cashier',
    is_active     TINYINT(1)   NOT NULL DEFAULT 1,
    last_login    DATETIME     NULL,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. Catalogue: Category -> Variety -> Product --------------------------------
CREATE TABLE Category (
    cid   INT AUTO_INCREMENT PRIMARY KEY,
    cname VARCHAR(100) NOT NULL UNIQUE
);

CREATE TABLE Variety (
    vid   INT AUTO_INCREMENT PRIMARY KEY,
    vname VARCHAR(150) NOT NULL,
    cid   INT NOT NULL,
    UNIQUE KEY uq_variety (cid, vname),
    FOREIGN KEY (cid) REFERENCES Category(cid) ON DELETE CASCADE
);

CREATE TABLE Product (
    pid           INT AUTO_INCREMENT PRIMARY KEY,
    pname         VARCHAR(150) NOT NULL UNIQUE,
    vid           INT NOT NULL,
    price         DECIMAL(10, 2) NOT NULL CHECK (price >= 0),
    cost_price    DECIMAL(10, 2) NOT NULL DEFAULT 0 CHECK (cost_price >= 0),
    reorder_level INT NOT NULL DEFAULT 20 CHECK (reorder_level >= 0),
    is_active     TINYINT(1) NOT NULL DEFAULT 1,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (vid) REFERENCES Variety(vid)
);

CREATE TABLE Stock (
    sid        INT AUTO_INCREMENT PRIMARY KEY,
    pid        INT NOT NULL UNIQUE,
    qty        INT NOT NULL DEFAULT 0 CHECK (qty >= 0),
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (pid) REFERENCES Product(pid) ON DELETE CASCADE
);

-- 3. Customers ----------------------------------------------------------------
CREATE TABLE Customer (
    cust_id    INT AUTO_INCREMENT PRIMARY KEY,
    cname      VARCHAR(100) NOT NULL,
    email      VARCHAR(150) NULL,
    phone_no   VARCHAR(20)  NULL,
    gender     VARCHAR(10)  NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_customer_phone (phone_no)
);

-- 3b. Discount offers - created by the OWNER, applied by cashiers at billing ------
CREATE TABLE Offer (
    offer_id        INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(100) NOT NULL UNIQUE,
    discount_type   ENUM('percent', 'flat') NOT NULL,
    value           DECIMAL(10, 2) NOT NULL CHECK (value > 0),   -- % or rupees, depending on type
    min_bill        DECIMAL(10, 2) NOT NULL DEFAULT 0,           -- bill must be at least this much
    min_total_spent DECIMAL(10, 2) NOT NULL DEFAULT 0,           -- customer must have shopped this much before
    max_discount    DECIMAL(10, 2) NULL,                         -- optional cap for percentage offers
    valid_from      DATE NULL,
    valid_to        DATE NULL,
    is_active       TINYINT(1) NOT NULL DEFAULT 1,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 4. Bills --------------------------------------------------------------------
CREATE TABLE Orders (
    oid             INT AUTO_INCREMENT PRIMARY KEY,
    bill_no         VARCHAR(30) NULL UNIQUE,          -- e.g. GB-20261101-00042
    cust_id         INT NULL,                         -- NULL = walk-in customer
    cashier_id      INT NULL,
    odate           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    subtotal        DECIMAL(10, 2) NOT NULL,
    discount        DECIMAL(10, 2) NOT NULL DEFAULT 0,
    total_amt       DECIMAL(10, 2) NOT NULL,
    payment_method  VARCHAR(20) NOT NULL DEFAULT 'Cash',
    amount_received DECIMAL(10, 2) NULL,
    offer_id        INT NULL,                         -- discount offer used (if any)
    offer_name      VARCHAR(100) NULL,                -- copied, so renaming an offer never changes old bills
    status          ENUM('active', 'voided') NOT NULL DEFAULT 'active',
    voided_at       DATETIME NULL,
    voided_by       INT NULL,
    void_reason     VARCHAR(255) NULL,
    INDEX idx_orders_date (odate),
    INDEX idx_orders_cashier (cashier_id),
    INDEX idx_orders_status (status),
    FOREIGN KEY (cust_id)    REFERENCES Customer(cust_id)     ON DELETE SET NULL,
    FOREIGN KEY (cashier_id) REFERENCES System_Users(user_id) ON DELETE SET NULL,
    FOREIGN KEY (offer_id)   REFERENCES Offer(offer_id)       ON DELETE SET NULL,
    FOREIGN KEY (voided_by)  REFERENCES System_Users(user_id) ON DELETE SET NULL
);

-- Prices and costs are copied onto each line, so later price changes never
-- alter historical bills or profit figures.
CREATE TABLE Order_item (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    oid        INT NOT NULL,
    pid        INT NOT NULL,
    qty        INT NOT NULL CHECK (qty > 0),
    unit_price DECIMAL(10, 2) NOT NULL,
    unit_cost  DECIMAL(10, 2) NOT NULL DEFAULT 0,
    line_total DECIMAL(10, 2) NOT NULL,
    INDEX idx_item_pid (pid),
    FOREIGN KEY (oid) REFERENCES Orders(oid) ON DELETE CASCADE,
    FOREIGN KEY (pid) REFERENCES Product(pid)
);

-- 5. Logs ----------------------------------------------------------------------
CREATE TABLE Stock_log (
    log_id           INT AUTO_INCREMENT PRIMARY KEY,
    pid              INT NOT NULL,
    change_qty       INT NOT NULL,
    qty_after        INT NOT NULL,
    transaction_type VARCHAR(50) NOT NULL,            -- SALE / VOID / RESTOCK / ADJUSTMENT / OPENING STOCK
    note             VARCHAR(255) NULL,
    user_id          INT NULL,
    timestamp        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_stocklog_time (timestamp),
    FOREIGN KEY (pid)     REFERENCES Product(pid)          ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES System_Users(user_id) ON DELETE SET NULL
);

CREATE TABLE Audit_log (
    log_id     INT AUTO_INCREMENT PRIMARY KEY,
    user_id    INT NULL,
    username   VARCHAR(50) NOT NULL DEFAULT '',
    action     VARCHAR(50) NOT NULL,
    details    TEXT NULL,
    ip_address VARCHAR(45) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_audit_time (created_at),
    INDEX idx_audit_action (action)
);

-- ============================================================================
--  STARTER DATA  (safe to edit or delete from the app later)
-- ============================================================================
INSERT INTO Category (cid, cname) VALUES
(1, 'Rice'), (2, 'Wheat'), (3, 'Pulses & Lentils'), (4, 'Millets');

INSERT INTO Variety (vid, vname, cid) VALUES
(1, 'Basmati Rice', 1), (2, 'Sona Masoori Rice', 1), (3, 'Kolam Rice', 1),
(4, 'Sharbati Wheat', 2), (5, 'Lokwan Wheat', 2), (6, 'Durum (Suji) Wheat', 2),
(7, 'Chana Dal (Bengal Gram)', 3), (8, 'Masoor Dal (Red Lentil)', 3), (9, 'Toor Dal (Pigeon Pea)', 3),
(10, 'Ragi (Finger Millet)', 4), (11, 'Jowar (Sorghum)', 4);

--                                 pid, name, vid, selling price, cost price
INSERT INTO Product (pid, pname, vid, price, cost_price) VALUES
(1,  'Lal Qilla Traditional Basmati Rice 5kg', 1, 650.00, 570.00),
(2,  'India Gate Feast Basmati Rice 5kg',      1, 450.00, 395.00),
(3,  'Dehraduni Sugandhi Basmati 1kg',         1, 110.00,  96.00),
(4,  'Royal Harvest Premium Sona Masoori 5kg', 2, 320.00, 280.00),
(5,  'Sri Lalitha Sona Masoori Rice 10kg',     2, 590.00, 520.00),
(6,  'Wada Kolam Rice 5kg',                    3, 280.00, 245.00),
(7,  'Bullet HMT Kolam Rice 1kg',              3,  62.00,  54.00),
(8,  'Aashirvaad Sharbati Atta (Flour) 5kg',   4, 260.00, 228.00),
(9,  'Fortune Sharbati Wheat Grains 10kg',     4, 490.00, 430.00),
(10, 'Standard Lokwan Whole Wheat 10kg',       5, 420.00, 370.00),
(11, 'Premium Wheat Suji (Semolina) 1kg',      6,  52.00,  45.00),
(12, 'Tata Sampann Chana Dal 1kg',             7, 115.00, 100.00),
(13, 'Organic Chana Dal Loose 1kg',            7,  98.00,  85.00),
(14, 'Premium Unpolished Masoor Dal 1kg',      8, 120.00, 105.00),
(15, 'Tata Sampann Unpolished Toor Dal 1kg',   9, 165.00, 145.00),
(16, 'Popular Toor Dal Loose 1kg',             9, 140.00, 122.00),
(17, 'Finger Ragi (Whole) 1kg',               10,  58.00,  50.00),
(18, 'Aashirvaad Multi-Grain Ragi Flour 1kg', 10,  72.00,  63.00),
(19, 'Organic Jowar (Sorghum) Grains 1kg',    11,  64.00,  56.00);

INSERT INTO Stock (pid, qty) VALUES
(1, 200), (2, 250), (3, 400), (4, 150), (5, 120),
(6, 180), (7, 500), (8, 300), (9, 160), (10, 140),
(11, 350), (12, 400), (13, 300), (14, 450), (15, 200),
(16, 300), (17, 150), (18, 150), (19, 180);

INSERT INTO Stock_log (pid, change_qty, qty_after, transaction_type, note)
SELECT pid, qty, qty, 'OPENING STOCK', 'Initial setup' FROM Stock;

INSERT INTO Customer (cust_id, cname, email, phone_no, gender) VALUES
(1,  'Rajesh Kumar',  'rajesh.kumar@email.com',  '9892301234', 'Male'),
(2,  'Priya Sharma',  'priya.sharma@email.com',  '9812345678', 'Female'),
(3,  'Amit Patel',    'amit.patel@email.com',    '9765432109', 'Male'),
(4,  'Sunita Rao',    'sunita.rao@email.com',    '9123456780', 'Female'),
(5,  'Vikram Singh',  'vikram.singh@email.com',  '9988776655', 'Male'),
(6,  'Deepa Nair',    'deepa.nair@email.com',    '9444332211', 'Female'),
(7,  'Sanjay Gupta',  'sanjay.gupta@email.com',  '9333555777', 'Male'),
(8,  'Anjali Verma',  'anjali.verma@email.com',  '9555666111', 'Female'),
(9,  'Rahul Bose',    'rahul.bose@email.com',    '9222444888', 'Male'),
(10, 'Kavita Mishra', 'kavita.mishra@email.com', '9666888222', 'Female'),
(11, 'Preeti Jha',    'preeti.jha@email.com',    '9111333555', 'Female'),
(12, 'Harish Joshi',  'harish.joshi@email.com',  '9888444333', 'Male');

-- Example offers - the owner can edit, switch off or delete these in the app.
INSERT INTO Offer (name, discount_type, value, min_bill, min_total_spent, max_discount) VALUES
('Rs.100 off on bills of Rs.1000+', 'flat',    100, 1000,    0, NULL),
('10% festival discount',           'percent',  10,  500,    0, 300),
('Loyal customer 5%',               'percent',   5,    0, 5000, NULL);

-- Optional: run backend_api/seed_demo.py to generate ~60 days of sample bills so
-- the owner analytics have something to show.
