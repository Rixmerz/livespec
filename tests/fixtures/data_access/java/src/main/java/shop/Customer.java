package shop;

import jakarta.persistence.Entity;
import jakarta.persistence.Table;

@Entity
@Table(name = "customers", schema = "crm")
public class Customer {
    Long id;
}
