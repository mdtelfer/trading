-- Extensiones base (presentes en la imagen de Timescale)
CREATE EXTENSION IF NOT EXISTS plpgsql;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Toolkit: solo si está disponible en el servidor
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb_toolkit') THEN
    EXECUTE 'CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit';
  ELSE
    RAISE NOTICE 'timescaledb_toolkit no disponible en este entorno; se omite.';
  END IF;
END$$;
