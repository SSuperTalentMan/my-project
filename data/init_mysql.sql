-- ============================================================
-- 「商枢」CommercePivot — MySQL 初始化脚本
-- 对应架构文档 §6 数据模型 与 §16 MySQL 初始化 SQL
-- 执行方式：
--   1) 用任意 MySQL 客户端直接导入本文件；或
--   2) python -m commercepivot.cli migrate   （推荐，幂等）
-- 字符集：utf8mb4 / utf8mb4_unicode_ci
-- ============================================================

CREATE DATABASE IF NOT EXISTS commercepivot
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE commercepivot;

-- ------------------------------------------------------------ 商品表
CREATE TABLE IF NOT EXISTS products (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(255) NOT NULL,
  category VARCHAR(100),
  price DECIMAL(10,2),
  platform VARCHAR(50),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  KEY idx_products_category (category),
  KEY idx_products_platform (platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ 订单表
CREATE TABLE IF NOT EXISTS orders (
  id INT PRIMARY KEY AUTO_INCREMENT,
  order_no VARCHAR(64) UNIQUE NOT NULL,
  user_id VARCHAR(64),
  product_id INT,
  amount DECIMAL(10,2),
  status VARCHAR(32),
  platform VARCHAR(50),
  order_date DATETIME,
  region VARCHAR(100),
  CONSTRAINT fk_orders_product FOREIGN KEY (product_id) REFERENCES products(id),
  KEY idx_orders_date (order_date),
  KEY idx_orders_platform (platform),
  KEY idx_orders_status (status),
  KEY idx_orders_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ 订单明细
CREATE TABLE IF NOT EXISTS order_items (
  id INT PRIMARY KEY AUTO_INCREMENT,
  order_id INT NOT NULL,
  product_id INT,
  quantity INT DEFAULT 1,
  unit_price DECIMAL(10,2),
  subtotal DECIMAL(10,2),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders(id),
  KEY idx_items_order (order_id),
  KEY idx_items_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ 售后表
CREATE TABLE IF NOT EXISTS after_sales (
  id INT PRIMARY KEY AUTO_INCREMENT,
  order_id INT,
  type VARCHAR(50),
  status VARCHAR(32),
  refund_amount DECIMAL(10,2),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_after_sales_order FOREIGN KEY (order_id) REFERENCES orders(id),
  KEY idx_after_sales_type (type),
  KEY idx_after_sales_status (status),
  KEY idx_after_sales_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ 库存表
CREATE TABLE IF NOT EXISTS inventory (
  id INT PRIMARY KEY AUTO_INCREMENT,
  sku VARCHAR(64) UNIQUE NOT NULL,
  product_id INT,
  warehouse VARCHAR(100),
  quantity INT DEFAULT 0,
  safety_stock INT DEFAULT 10,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_inventory_product FOREIGN KEY (product_id) REFERENCES products(id),
  KEY idx_inventory_warehouse (warehouse)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ 广告报表
CREATE TABLE IF NOT EXISTS ad_reports (
  id INT PRIMARY KEY AUTO_INCREMENT,
  platform VARCHAR(50),
  report_date DATE,
  impressions INT,
  clicks INT,
  cost DECIMAL(10,2),
  revenue DECIMAL(10,2),
  KEY idx_ad_platform (platform),
  KEY idx_ad_date (report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ 会话表
CREATE TABLE IF NOT EXISTS chat_sessions (
  id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64),
  role VARCHAR(32),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sessions_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ 消息表
CREATE TABLE IF NOT EXISTS chat_messages (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  session_id VARCHAR(64),
  role VARCHAR(32),
  content TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_messages_session FOREIGN KEY (session_id) REFERENCES chat_sessions(id),
  KEY idx_messages_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ MCP 审计表
-- 说明：在 §16 原字段基础上增加了 request_id —— §12 要求「X-Request-ID 贯穿
-- route -> A2A -> MCP -> LLM」，若不入库则事后无法按 request_id 追溯整条链路。
CREATE TABLE IF NOT EXISTS mcp_audit (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  tool_name VARCHAR(100),
  params TEXT,
  result TEXT,
  elapsed_ms INT,
  status VARCHAR(32),
  request_id VARCHAR(64),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  KEY idx_audit_tool (tool_name),
  KEY idx_audit_created (created_at),
  KEY idx_audit_request (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ Agent 任务表
CREATE TABLE IF NOT EXISTS agent_tasks (
  id VARCHAR(64) PRIMARY KEY,
  agent_name VARCHAR(100),
  status VARCHAR(32),
  input TEXT,
  output TEXT,
  elapsed_ms INT,
  request_id VARCHAR(64),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  KEY idx_tasks_agent (agent_name),
  KEY idx_tasks_status (status),
  KEY idx_tasks_request (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- 以下为「支撑文档中声明的降级路径」所必需的补充表
-- 依据：§11「Milvus Lite 不可用：RAG 降级为 MySQL LIKE 或 BM25」
--       —— 知识文本必须在 MySQL 留一份镜像，否则该降级无法实现。
-- ============================================================

-- 知识镜像表：与 Milvus 集合 product_kb / faq_kb 一一对应
CREATE TABLE IF NOT EXISTS knowledge_docs (
  id VARCHAR(96) PRIMARY KEY,
  collection VARCHAR(32) NOT NULL,
  title VARCHAR(255),
  content TEXT NOT NULL,
  source VARCHAR(255),
  keywords VARCHAR(255),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  KEY idx_kb_collection (collection)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 人工工单表（§5 工具 create_ticket 的落地表，写操作默认拒绝）
CREATE TABLE IF NOT EXISTS tickets (
  id VARCHAR(64) PRIMARY KEY,
  summary VARCHAR(255) NOT NULL,
  priority VARCHAR(16) DEFAULT 'P2',
  contact VARCHAR(128),
  session_id VARCHAR(64),
  status VARCHAR(32) DEFAULT 'open',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  KEY idx_tickets_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- 可选优化：中文全文索引（ngram），用于 BM25 降级检索
-- 若当前 MySQL 未启用 ngram 解析器，本语句会失败，
-- 通过 cli migrate 执行时会自动跳过，不影响 LIKE 降级。
-- ============================================================
-- ALTER TABLE knowledge_docs ADD FULLTEXT INDEX ft_knowledge (title, content) WITH PARSER ngram;
