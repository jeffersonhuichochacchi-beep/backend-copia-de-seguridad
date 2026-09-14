-- ===========================
-- Base de Datos: tienda_db
-- Sistema: PostgreSQL
-- ===========================

-- Crear base de datos (ejecutar esto primero si no existe)
-- CREATE DATABASE tienda_db;

-- Conectar a la base de datos
-- \c tienda_db

-- Tabla Productos
CREATE TABLE IF NOT EXISTS productos (
    id_producto SERIAL PRIMARY KEY,
    nombre VARCHAR(255) NOT NULL,
    precio DECIMAL(10, 2) NOT NULL,
    stock INTEGER NOT NULL
);

-- Tabla Clientes
CREATE TABLE IF NOT EXISTS clientes (
    id_cliente SERIAL PRIMARY KEY,
    nombre VARCHAR(100) NOT NULL,
    apellido VARCHAR(100) NOT NULL,
    email VARCHAR(255) UNIQUE
);

-- Tabla Pedidos
CREATE TABLE IF NOT EXISTS pedidos (
    id_pedido SERIAL PRIMARY KEY,
    id_cliente INTEGER NOT NULL,
    id_producto INTEGER NOT NULL,
    cantidad INTEGER NOT NULL,
    fecha_pedido DATE NOT NULL,
    FOREIGN KEY (id_cliente) REFERENCES clientes(id_cliente),
    FOREIGN KEY (id_producto) REFERENCES productos(id_producto)
);

-- Insertar datos de ejemplo en Productos
INSERT INTO productos (nombre, precio, stock) VALUES
('Laptop', 850.00, 20),
('Ratón Inalámbrico', 25.50, 100),
('Teclado Mecánico', 75.00, 50);

-- Insertar datos de ejemplo en Clientes
INSERT INTO clientes (nombre, apellido, email) VALUES
('Ana', 'García', 'ana.garcia@gmail.com'),
('Luis', 'Rodríguez', 'luis.rodriguez@gmail.com');

-- Insertar datos de ejemplo en Pedidos
INSERT INTO pedidos (id_cliente, id_producto, cantidad, fecha_pedido)
VALUES
(1, 1, 1, '2024-05-20'),
(1, 2, 2, '2024-05-20'),
(2, 3, 1, '2024-05-21');

-- Verificar datos
SELECT 'Productos:' as tabla, COUNT(*) as total FROM productos
UNION ALL
SELECT 'Clientes:' as tabla, COUNT(*) as total FROM clientes
UNION ALL
SELECT 'Pedidos:' as tabla, COUNT(*) as total FROM pedidos;
