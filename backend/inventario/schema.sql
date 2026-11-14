-- Esquema de inventario. Idempotente: la Lambda migrate lo ejecuta en cada despliegue.

-- Árbol de categorías (lista de adyacencia). Una sola raíz; el dominio rechaza ciclos al construir el árbol.
CREATE TABLE IF NOT EXISTS categories (
  id        INT PRIMARY KEY,
  name      VARCHAR(80) NOT NULL,
  parent_id INT NULL,
  CONSTRAINT chk_not_self CHECK (parent_id IS NULL OR parent_id <> id),
  FOREIGN KEY (parent_id) REFERENCES categories(id),
  INDEX idx_cat_parent (parent_id)
);

CREATE TABLE IF NOT EXISTS products (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  sku             VARCHAR(32)  NOT NULL UNIQUE,
  name            VARCHAR(120) NOT NULL,
  category_id     INT NULL,
  stock           INT NOT NULL,
  threshold       INT NOT NULL,
  reorder_qty     INT NOT NULL,
  pending_orders  INT NOT NULL DEFAULT 0,
  lead_time_hours INT NOT NULL DEFAULT 30,
  history_head    INT NOT NULL DEFAULT 0,  -- cabeza del buffer circular de stock_readings
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT chk_stock     CHECK (stock >= 0),
  CONSTRAINT chk_threshold CHECK (threshold >= 0),
  CONSTRAINT chk_reorder   CHECK (reorder_qty > 0),
  CONSTRAINT chk_pending   CHECK (pending_orders >= 0),
  CONSTRAINT chk_lead      CHECK (lead_time_hours > 0),
  FOREIGN KEY (category_id) REFERENCES categories(id),
  INDEX idx_low_stock (pending_orders, stock, threshold)
);

-- Buffer circular persistido: 24 ranuras fijas por producto. slot = history_head % 24.
-- Nunca crece: cada lectura nueva sobrescribe la más antigua.
CREATE TABLE IF NOT EXISTS stock_readings (
  product_id INT NOT NULL,
  slot       TINYINT NOT NULL,
  stock      INT NOT NULL,
  read_at    DATETIME NOT NULL,
  PRIMARY KEY (product_id, slot),
  CONSTRAINT chk_slot CHECK (slot BETWEEN 0 AND 23),
  FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS restock_requests (
  id         CHAR(36) PRIMARY KEY,
  product_id INT NOT NULL,
  qty        INT NOT NULL,
  source     ENUM('SCHEDULER','MANUAL') NOT NULL,
  status     ENUM('QUEUED','ORDERED') NOT NULL DEFAULT 'QUEUED',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT chk_req_qty CHECK (qty BETWEEN 1 AND 10000),
  FOREIGN KEY (product_id) REFERENCES products(id),
  INDEX idx_req_status (status, created_at)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_id  CHAR(36) NOT NULL UNIQUE,  -- una orden por solicitud: idempotencia también en la BD
  product_id  INT NOT NULL,
  qty         INT NOT NULL,
  status      ENUM('CREATED','SENT','RECEIVED') NOT NULL DEFAULT 'CREATED',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  received_at TIMESTAMP NULL,
  FOREIGN KEY (request_id) REFERENCES restock_requests(id),
  FOREIGN KEY (product_id) REFERENCES products(id),
  INDEX idx_po_created (created_at)
);

CREATE TABLE IF NOT EXISTS agent_insights (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  severity   ENUM('INFO','MEDIA','ALTA') NOT NULL,
  summary    VARCHAR(1000) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT IGNORE INTO categories (id, name, parent_id) VALUES
  (1, 'Bodega',               NULL),
  (2, 'Fijaciones',           1),
  (3, 'Tornillería',          2),
  (4, 'Tuercas y arandelas',  2),
  (5, 'Herramientas',         1),
  (6, 'Brocas',               5),
  (7, 'Eléctricos',           1);

INSERT IGNORE INTO products (sku, name, category_id, stock, threshold, reorder_qty) VALUES
  ('SKU-001', 'Tornillo 3/8',     3, 420, 100, 300),
  ('SKU-002', 'Tuerca hexagonal', 4,  35,  80, 200),
  ('SKU-003', 'Arandela plana',   4, 210, 150, 250),
  ('SKU-004', 'Broca 6mm',        6,  12,  20,  50),
  ('SKU-005', 'Cinta aislante',   7,  90,  40, 100);
