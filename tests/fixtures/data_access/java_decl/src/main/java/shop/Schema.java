package shop;

import java.sql.Connection;
import java.sql.SQLException;

public class Schema {
    private static final String DDL = "CREATE TABLE cogs (id INTEGER PRIMARY KEY, teeth INTEGER)";

    public void init(Connection conn) throws SQLException {
        conn.createStatement().execute(DDL);
    }
}
