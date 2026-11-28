package shop;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

public interface CustomerRepository extends JpaRepository<Customer, Long> {
    List<Customer> findByName(String name);

    @Query("SELECT c FROM Customer c WHERE c.name = ?1")
    List<Customer> byName(String name);

    @Query(value = "SELECT * FROM crm.customers c JOIN orders o ON o.cid = c.id", nativeQuery = true)
    List<Customer> withOrders();
}
