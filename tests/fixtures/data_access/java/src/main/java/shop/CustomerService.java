package shop;

import jakarta.persistence.EntityManager;

public class CustomerService {
    private final CustomerRepository customers;
    private final EntityManager em;

    public CustomerService(CustomerRepository customers, EntityManager em) {
        this.customers = customers;
        this.em = em;
    }

    public void rename(String name) {
        customers.findByName(name);
        customers.save(new Customer());
    }

    public void saveBasket(Basket b) {
        em.persist(b);
    }

    public Customer load(long id) {
        return em.find(Customer.class, id);
    }

    public int purge() {
        return em.createQuery("DELETE FROM Customer c WHERE c.id < 0").executeUpdate();
    }

    public String sql() {
        String q = "UPDATE legacy_users SET active = 0 "
            + "WHERE id = " + 7;
        return q;
    }

    public String text() {
        return "Delete from the cart";
    }
}
